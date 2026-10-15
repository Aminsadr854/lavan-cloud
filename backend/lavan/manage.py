"""Explicit local development administration. Issued tokens go only to stdout."""

import argparse
import json
import sys
from uuid import UUID

from sqlalchemy import func, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from lavan.db import database_engine
from lavan.models import ApiToken, User
from lavan.tokens import new_token, token_digest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("create-user")
    commands.add_parser("issue-token").add_argument("user_id", type=UUID)
    commands.add_parser("revoke-token").add_argument("token_id", type=UUID)
    args = parser.parse_args(argv)
    engine = database_engine()
    try:
        with Session(engine, expire_on_commit=False) as session:
            if args.command == "create-user":
                user = User()
                session.add(user)
                session.commit()
                result = {
                    "user_id": str(user.id),
                    "created_at": user.created_at.isoformat(),
                }
            elif args.command == "issue-token":
                if session.get(User, args.user_id) is None:
                    raise LookupError("User not found.")
                raw = new_token()
                token = ApiToken(user_id=args.user_id, digest=token_digest(raw))
                session.add(token)
                session.commit()
                result = {
                    "token_id": str(token.id),
                    "user_id": str(args.user_id),
                    "token": raw,
                }
            else:
                token_id = session.scalar(
                    update(ApiToken)
                    .where(ApiToken.id == args.token_id)
                    .values(revoked_at=func.coalesce(ApiToken.revoked_at, func.now()))
                    .returning(ApiToken.id)
                )
                if token_id is None:
                    raise LookupError("Token not found.")
                session.commit()
                result = {"token_id": str(token_id), "revoked": True}
        # Only committed issuance reaches stdout. Never log these results.
        print(json.dumps(result))
        return 0
    except LookupError as error:
        print(str(error), file=sys.stderr)
        return 1
    except SQLAlchemyError:
        print("Database operation failed.", file=sys.stderr)
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
