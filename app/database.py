from collections.abc import Generator

from fastapi import Request
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base


class Database:
    def __init__(self, url: str) -> None:
        engine_options: dict = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            engine_options["connect_args"] = {"check_same_thread": False}
        if url in {"sqlite://", "sqlite:///:memory:"}:
            engine_options["poolclass"] = StaticPool

        self.engine = create_engine(url, **engine_options)
        self.session_factory = sessionmaker(
            bind=self.engine,
            autoflush=False,
            expire_on_commit=False,
        )

    def create_schema(self) -> None:
        if self.engine.url.drivername.startswith("sqlite"):
            Base.metadata.create_all(self.engine)

    def close(self) -> None:
        self.engine.dispose()


def get_session(request: Request) -> Generator[Session, None, None]:
    session = request.app.state.database.session_factory()
    try:
        yield session
    finally:
        session.close()

