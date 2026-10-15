"""Settings supply credentials; never serialize a URL into Alembic config."""

from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from lavan.config import Settings
from lavan.models import Base

config = context.config


def migrate(connection):
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(
        url=Settings().database_url,
        target_metadata=Base.metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    supplied_connection = config.attributes.get("connection")
    if supplied_connection is not None:
        migrate(supplied_connection)
    else:
        engine = create_engine(
            Settings().database_url,
            poolclass=NullPool,
            connect_args={
                "connect_timeout": 2,
                "options": "-c timezone=UTC -c statement_timeout=10000",
            },
            hide_parameters=True,
        )
        try:
            with engine.connect() as connection:
                migrate(connection)
        finally:
            engine.dispose()
