import time
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker


class Database:
    def __init__(self, url: str, cipher=None):
        if url.startswith("sqlite:///") and ":memory:" not in url:
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        options = (
            {"connect_args": {"check_same_thread": False, "timeout": 20}} if url.startswith("sqlite") else {}
        )
        self.engine = create_engine(url, pool_pre_ping=True, **options)
        self.cipher = cipher
        self.engine.dialect.rage_cipher = cipher
        if url.startswith("sqlite"):

            @event.listens_for(self.engine, "connect")
            def pragmas(connection, _):
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA secure_delete=ON")

        self.session = sessionmaker(self.engine, expire_on_commit=False)

    def migrate(self):
        from alembic import command
        from alembic.config import Config

        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
        with self.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")

    def instrument(self, monitoring):
        @event.listens_for(self.engine, "before_cursor_execute")
        def before(conn, cursor, statement, parameters, context, executemany):
            context.rag_started = time.perf_counter()

        @event.listens_for(self.engine, "after_cursor_execute")
        def after(conn, cursor, statement, parameters, context, executemany):
            monitoring.record("database", time.perf_counter() - context.rag_started)

        @event.listens_for(self.engine, "handle_error")
        def failed(context):
            started = getattr(context.execution_context, "rag_started", time.perf_counter())
            monitoring.record("database", time.perf_counter() - started, False)

    def healthy(self):
        with self.engine.connect() as conn:
            conn.execute(text("SELECT 1"))

    def close(self):
        self.engine.dispose()
