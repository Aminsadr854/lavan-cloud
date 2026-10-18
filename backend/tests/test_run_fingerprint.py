from uuid import uuid4

from lavan.runs import request_fingerprint


def test_fingerprint_includes_all_semantic_fields():
    project, artifact = uuid4(), uuid4()
    original = request_fingerprint(project, artifact, "python-fastapi-v1")
    assert len(original) == 64
    assert original == request_fingerprint(project, artifact, "python-fastapi-v1")
    assert original != request_fingerprint(uuid4(), artifact, "python-fastapi-v1")
    assert original != request_fingerprint(project, uuid4(), "python-fastapi-v1")
    assert original != request_fingerprint(project, artifact, "future-template")
