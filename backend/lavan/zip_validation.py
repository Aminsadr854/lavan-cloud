"""Bounded ZIP metadata validation. Never extract or decompress payloads."""

import re
import stat
import struct
import unicodedata


class InvalidZip(ValueError):
    pass


def require(condition):
    if not condition:
        raise InvalidZip("Invalid or unsupported ZIP")


def extra_fields(data):
    while data:
        require(len(data) >= 4)
        kind, length = struct.unpack_from("<HH", data)
        require(kind != 1 and length <= len(data) - 4)  # No ZIP64.
        data = data[4 + length :]


def normalized_name(raw, flags, settings):
    require(b"\0" not in raw)
    name = raw.decode("utf-8" if flags & 0x800 else "cp437")
    require(len(name) <= settings.zip_max_filename_chars)
    require(not any(unicodedata.category(c).startswith("C") for c in name))
    require(not any(c in name for c in '\\:<>"|?*'))
    name = unicodedata.normalize("NFC", name)
    directory = name.endswith("/")
    parts = name[:-1].split("/") if directory else name.split("/")
    reserved = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$", re.I)
    for part in parts:
        require(part not in ("", ".", "..") and not part.endswith((" ", ".")))
        require(not reserved.fullmatch(part.split(".")[0]))
    return "/".join(parts), directory


def validate_zip(path, settings):
    """Validate original header names, bounded directory records and byte ranges."""
    try:
        return _validate(path, settings)
    except (ValueError, UnicodeError, struct.error, OverflowError):
        raise InvalidZip("Invalid or unsupported ZIP") from None


def _validate(path, settings):
    size = path.stat().st_size
    with path.open("rb") as archive:
        archive.seek(max(0, size - 65557))
        tail = archive.read(65557)
        end = tail.rfind(b"PK\x05\x06")
        require(end >= 0 and len(tail) - end >= 22)
        sig, disk, cd_disk, count_disk, count, cd_size, cd_offset, comment = (
            struct.unpack_from("<4s4H2IH", tail, end)
        )
        require(end + 22 + comment == len(tail))
        require(disk == cd_disk == 0 and count_disk == count)
        require(0 < count <= settings.zip_max_entries and count < 65535)
        require(cd_offset + cd_size == size - len(tail) + end)
        require(cd_size >= count * 46)
        archive.seek(cd_offset)
        entries, directories, ranges = {}, set(), []
        total = files = 0
        for _ in range(count):
            fixed = archive.read(46)
            require(len(fixed) == 46)
            (
                sig,
                made,
                needed,
                flags,
                method,
                _,
                _,
                crc,
                compressed,
                declared,
                length,
                extra,
                comment,
                disk,
                _,
                attributes,
                offset,
            ) = struct.unpack("<4s6H3I5H2I", fixed)
            require(sig == b"PK\x01\x02" and disk == 0 and needed <= 20)
            require(flags & ~0x80E == 0 and method in (0, 8))
            require(compressed < 0xFFFFFFFF and declared < 0xFFFFFFFF)
            require(0 < length <= 4 * settings.zip_max_filename_chars)
            require(archive.tell() + length + extra + comment <= cd_offset + cd_size)
            raw = archive.read(length)
            extra_fields(archive.read(extra))
            archive.seek(comment, 1)
            next_entry = archive.tell()
            name, directory = normalized_name(raw, flags, settings)
            file_type = stat.S_IFMT(attributes >> 16)
            require(file_type in (0, stat.S_IFDIR, stat.S_IFREG))
            require(file_type != stat.S_IFDIR or directory)
            require(file_type != stat.S_IFREG or not directory)
            require(not attributes & 0x10 or directory)
            require(not attributes & 0x408)  # DOS volume label / reparse point.
            require(name not in entries)
            require(directory or name not in directories)
            parts = name.split("/")
            for index in range(1, len(parts)):
                parent = "/".join(parts[:index])
                require(entries.get(parent) != "file")
                directories.add(parent)
            entries[name] = "dir" if directory else "file"
            require(declared <= settings.zip_max_file_bytes)
            require(not directory or declared == 0)
            total += declared
            require(total <= settings.zip_max_uncompressed_bytes)
            files += not directory
            require(method != 0 or compressed == declared)
            require(offset + 30 <= cd_offset)
            archive.seek(offset)
            local = archive.read(30)
            (
                signature,
                local_needed,
                local_flags,
                local_method,
                _,
                _,
                local_crc,
                local_compressed,
                local_declared,
                local_length,
                local_extra,
            ) = struct.unpack("<4s5H3I2H", local)
            require(signature == b"PK\x03\x04" and local_needed == needed)
            require(local_flags == flags and local_method == method)
            require(local_length == length and archive.read(local_length) == raw)
            extra_fields(archive.read(local_extra))
            payload_end = archive.tell() + compressed
            require(payload_end <= cd_offset)
            if flags & 8:
                archive.seek(payload_end)
                descriptor = archive.read(4)
                if descriptor == b"PK\x07\x08":
                    descriptor = archive.read(12)
                    payload_end += 16
                else:
                    descriptor += archive.read(8)
                    payload_end += 12
                require(struct.unpack("<3I", descriptor) == (crc, compressed, declared))
                require(payload_end <= cd_offset)
            else:
                require(
                    (local_crc, local_compressed, local_declared)
                    == (crc, compressed, declared)
                )
            ranges.append((offset, payload_end))
            archive.seek(next_entry)
        require(archive.tell() == cd_offset + cd_size and files > 0)
        boundary = 0
        for start, end in sorted(ranges):
            require(start == boundary and end > start)
            boundary = end
        require(boundary == cd_offset)
        return files, total
