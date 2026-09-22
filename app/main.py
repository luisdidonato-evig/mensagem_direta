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

from app.analytics import process_analytics_outbox
from app.api import router
from app.database import Database
from app.domain import ActorRole
from app.integration import MetaCloudApiSender, OutboxProcessor, run_outbox_worker
from app.media import MetaMediaDownloader
from app.models import Agent, Company, GroupAgent, ServiceGroup
from app.realtime import ConnectionManager
from app.security import hash_password

logger = logging.getLogger(__name__)

DEV_SEED_PASSWORD = "dev-local-only"
DEV_COMPANY_ID = "00000000-0000-0000-0000-000000000001"
DEV_GROUP_ID = "00000000-0000-0000-0000-000000000001"
DEV_SEED_AGENTS = [
    ("agente-1", ActorRole.ATENDENTE),
    ("agente-2", ActorRole.ATENDENTE),
    ("supervisor-1", ActorRole.SUPERVISOR),
    ("admin-1", ActorRole.ADMIN),
]


async def run_analytics_worker(session_factory, interval_seconds: float) -> None:
    while True:
        with session_factory() as session:
            process_analytics_outbox(session)
        await asyncio.sleep(interval_seconds)


def seed_dev_agents(database: Database) -> None:
    """Semente só para simulador/dev/testes — nunca roda com ENABLE_SIMULATOR=false."""
    with database.session_factory() as session:
        if session.get(Company, DEV_COMPANY_ID) is None:
            session.add(Company(id=DEV_COMPANY_ID, name="Empresa de desenvolvimento"))
        if session.get(ServiceGroup, DEV_GROUP_ID) is None:
            session.add(
                ServiceGroup(
                    id=DEV_GROUP_ID,
                    company_id=DEV_COMPANY_ID,
                    name="Central",
                )
            )
        for agent_id, role in DEV_SEED_AGENTS:
            if session.get(Agent, agent_id) is None:
                session.add(
                    Agent(
                        id=agent_id,
                        company_id=DEV_COMPANY_ID,
                        role=role,
                        password_hash=hash_password(DEV_SEED_PASSWORD),
                    )
                )
            if session.get(GroupAgent, (DEV_GROUP_ID, agent_id)) is None:
                session.add(GroupAgent(group_id=DEV_GROUP_ID, agent_id=agent_id))
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
    media_storage_dir: str | None = None,
) -> FastAPI:
    project_root = Path(__file__).resolve().parent.parent
    resolved_url = database_url or os.getenv(
        "DATABASE_URL", "sqlite:///./data/centro_atendimento.db"
    )
    if resolved_url.startswith("sqlite:///./"):
        Path("data").mkdir(exist_ok=True)

    database = Database(resolved_url)
    resolved_media_dir = Path(
        media_storage_dir
        or os.getenv("MEDIA_STORAGE_DIR")
        or (
            Path(database.engine.url.database).parent / "media"
            if database.engine.url.drivername.startswith("sqlite")
            and database.engine.url.database not in {None, ":memory:"}
            else project_root / "data" / "media"
        )
    ).resolve()
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
                media_storage_dir=resolved_media_dir,
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
        analytics_worker = asyncio.create_task(
            run_analytics_worker(database.session_factory, 5.0)
        )
        yield
        if worker:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
        analytics_worker.cancel()
        with suppress(asyncio.CancelledError):
            await analytics_worker
        database.close()

    app = FastAPI(
        title="Centro de Atendimento",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.database = database
    app.state.realtime = ConnectionManager(database.session_factory)
    app.state.meta_verify_token = resolved_verify_token
    app.state.meta_app_secret = resolved_app_secret
    app.state.enable_simulator = simulator_enabled
    app.state.outbox_processor = outbox_processor
    app.state.jwt_secret = resolved_jwt_secret
    app.state.media_storage_dir = resolved_media_dir
    app.state.meta_media_downloader = MetaMediaDownloader(
        resolved_access_token, resolved_graph_version
    )
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
