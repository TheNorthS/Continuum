from alembic import context
from sqlalchemy import create_engine

from app.common.encryption import Encrypted
from app.config import Settings
from app.models.entities import Base


def run(connection):
    def compare_type(context, inspected_column, metadata_column, inspected_type, metadata_type):
        if connection.dialect.name == "sqlite" and isinstance(metadata_type, Encrypted):
            # SQLite dynamically stores TEXT in legacy JSON/VARCHAR columns. Avoid destructive table rebuilds.
            return False
        return None

    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=compare_type)
    with context.begin_transaction():
        context.run_migrations()


provided = context.config.attributes.get("connection")
if provided is not None:
    run(provided)
else:
    engine = create_engine(Settings().database_url)
    with engine.connect() as connection:
        run(connection)
    engine.dispose()
