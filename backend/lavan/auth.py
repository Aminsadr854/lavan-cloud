from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from lavan.models import ApiToken, User
from lavan.tokens import TOKEN_PATTERN, token_digest


def get_session(request: Request):
    try:
        with Session(request.app.state.auth_engine) as session:
            yield session
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="Service unavailable") from None


def authenticated_user(
    session: Annotated[Session, Depends(get_session)],
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    unauthorized = HTTPException(
        status_code=401,
        detail="Invalid credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if authorization is None:
        raise unauthorized
    scheme, separator, token = authorization.partition(" ")
    if (
        not separator
        or scheme.lower() != "bearer"
        or not TOKEN_PATTERN.fullmatch(token)
    ):
        raise unauthorized
    user = session.scalar(
        select(User)
        .join(ApiToken, ApiToken.user_id == User.id)
        .where(ApiToken.digest == token_digest(token), ApiToken.revoked_at.is_(None))
    )
    if user is None:
        raise unauthorized
    return user
