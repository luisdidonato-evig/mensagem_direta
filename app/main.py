import logging
import os
import asyncio
import secrets
from contextlib import asynccontextmanager
from contextlib import suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select

from app.api import router
from app.database import Database
from app.domain import ActorRole
from app.integration import MetaCloudApiSender, OutboxProcessor, run_outbox_worker
from app.models import Agent
from app.realtime import ConnectionManager
from app.security import hash_password

logger = logging.getLogger(__name__)

DEV_SEED_PASSWORD = "dev-local-only"
DEV_SEED_AGENTS = [
    ("agente-1", ActorRole.ATENDENTE),
    ("agente-2", ActorRole.ATENDENTE),
    ("supervisor-1", ActorRole.SUPERVISOR),
    ("admin-1", ActorRole.ADMIN),
]


def seed_dev_agents(database: Database) -> None:
    """Semente só para simulador/dev/testes — nunca roda com ENABLE_SIMULATOR=false."""
    with database.session_factory() as session:
        if session.scalar(select(Agent.id).limit(1)) is not None:
            return
        session.add_all(
            [
                Agent(id=agent_id, role=role, password_hash=hash_password(DEV_SEED_PASSWORD))
                for agent_id, role in DEV_SEED_AGENTS
            ]
        )
        session.commit()


def create_app(
    database_url: str | None = None,
    meta_verify_token: str | None = None,
    meta_app_secret: str | None = None,
    meta_access_token: str | None = None,
    meta_phone_number_id: str | None = None,
    meta_graph_version: str | None = None,
    enable_simulator: bool | None = None,
    jwt_secret: str | None = None,
) -> FastAPI:
    project_root = Path(__file__).resolve().parent.parent
    resolved_url = database_url or os.getenv(
        "DATABASE_URL", "sqlite:///./data/centro_atendimento.db"
    )
    if resolved_url.startswith("sqlite:///./"):
        Path("data").mkdir(exist_ok=True)

    database = Database(resolved_url)
    resolved_verify_token = meta_verify_token or os.getenv("META_VERIFY_TOKEN", "")
    resolved_app_secret = meta_app_secret or os.getenv("META_APP_SECRET", "")
    resolved_access_token = meta_access_token or os.getenv("META_ACCESS_TOKEN", "")
    resolved_phone_number_id = meta_phone_number_id or os.getenv("META_PHONE_NUMBER_ID", "")
    resolved_graph_version = meta_graph_version or os.getenv("META_GRAPH_VERSION", "v23.0")
    simulator_enabled = enable_simulator
    if simulator_enabled is None:
        simulator_enabled = os.getenv("ENABLE_SIMULATOR", "true").lower() == "true"
    resolved_jwt_secret = jwt_secret or os.getenv("JWT_SECRET")
    if not resolved_jwt_secret:
        resolved_jwt_secret = secrets.token_urlsafe(32)
        logger.warning(
            "JWT_SECRET não configurado — usando segredo efêmero gerado em memória; "
            "tokens ficam inválidos a cada reinício. Defina JWT_SECRET antes de produção."
        )
    outbox_processor = None
    if resolved_access_token and resolved_phone_number_id:
        outbox_processor = OutboxProcessor(
            database.session_factory,
            MetaCloudApiSender(
                phone_number_id=resolved_phone_number_id,
                access_token=resolved_access_token,
                graph_version=resolved_graph_version,
            ),
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database.create_schema()
        if simulator_enabled:
            seed_dev_agents(database)
        worker = None
        if outbox_processor:
            worker = asyncio.create_task(run_outbox_worker(outbox_processor, 2.0))
        yield
        if worker:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
        database.close()

    app = FastAPI(
        title="Centro de Atendimento",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.database = database
    app.state.realtime = ConnectionManager()
    app.state.meta_verify_token = resolved_verify_token
    app.state.meta_app_secret = resolved_app_secret
    app.state.enable_simulator = simulator_enabled
    app.state.outbox_processor = outbox_processor
    app.state.jwt_secret = resolved_jwt_secret
    app.include_router(router)
    app.mount("/static", StaticFiles(directory=project_root / "static"), name="static")

    @app.get("/", include_in_schema=False)
    def panel() -> FileResponse:
        return FileResponse(project_root / "static" / "index.html")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
