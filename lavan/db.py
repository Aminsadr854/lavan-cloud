from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from lavan.config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True)
Session = sessionmaker(engine, expire_on_commit=False)


def session():
    with Session() as db:
        yield db
