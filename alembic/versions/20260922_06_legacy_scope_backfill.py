"""Migrate the original single-queue SQLite database to company/group scope.

Revision ID: 20260922_06
Revises: 20260922_05
"""
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260922_06"
down_revision: Union[str, None] = "20260922_05"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

LEGACY_COMPANY_ID = "00000000-0000-0000-0000-000000000001"
LEGACY_GROUP_ID = "00000000-0000-0000-0000-000000000001"


def _add_if_missing(table: str, column: sa.Column, index: bool = False) -> None:
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}
    if column.name not in columns:
        op.add_column(table, column)
        if index:
            op.create_index(f"ix_{table}_{column.name}", table, [column.name])


def upgrade() -> None:
    _add_if_missing("agents", sa.Column("company_id", sa.String(36), nullable=True), True)
    _add_if_missing("attendances", sa.Column("company_id", sa.String(36), nullable=True), True)
    _add_if_missing("attendances", sa.Column("group_id", sa.String(36), nullable=True), True)
    _add_if_missing(
        "attendances",
        sa.Column("load_weight", sa.Integer(), nullable=False, server_default="1"),
    )

    bind = op.get_bind()
    legacy_agents = bind.scalar(sa.text("SELECT COUNT(*) FROM agents WHERE company_id IS NULL"))
    legacy_attendances = bind.scalar(
        sa.text("SELECT COUNT(*) FROM attendances WHERE company_id IS NULL")
    )
    if not legacy_agents and not legacy_attendances:
        return

    now = datetime.now(timezone.utc)
    if bind.scalar(
        sa.text("SELECT id FROM companies WHERE id = :id"), {"id": LEGACY_COMPANY_ID}
    ) is None:
        bind.execute(
            sa.text(
                "INSERT INTO companies (id, name, active, created_at, updated_at) "
                "VALUES (:id, :name, :active, :now, :now)"
            ),
            {"id": LEGACY_COMPANY_ID, "name": "EVIG", "active": True, "now": now},
        )
    if bind.scalar(
        sa.text("SELECT id FROM service_groups WHERE id = :id"), {"id": LEGACY_GROUP_ID}
    ) is None:
        bind.execute(
            sa.text(
                "INSERT INTO service_groups "
                "(id, company_id, name, active, max_load_per_agent, created_at, updated_at) "
                "VALUES (:id, :company_id, :name, :active, :capacity, :now, :now)"
            ),
            {
                "id": LEGACY_GROUP_ID,
                "company_id": LEGACY_COMPANY_ID,
                "name": "Central",
                "active": True,
                "capacity": 5,
                "now": now,
            },
        )

    bind.execute(
        sa.text("UPDATE agents SET company_id = :id WHERE company_id IS NULL"),
        {"id": LEGACY_COMPANY_ID},
    )
    bind.execute(
        sa.text(
            "UPDATE attendances SET company_id = :company_id, group_id = :group_id "
            "WHERE company_id IS NULL"
        ),
        {"company_id": LEGACY_COMPANY_ID, "group_id": LEGACY_GROUP_ID},
    )
    bind.execute(
        sa.text(
            "UPDATE messages SET sender_type = 'CLIENTE' "
            "WHERE direction = 'ENTRADA' AND sender_type = 'SISTEMA'"
        )
    )
    bind.execute(
        sa.text(
            "UPDATE messages SET sender_type = 'ATENDENTE' "
            "WHERE direction = 'SAIDA' AND sender_type = 'SISTEMA'"
        )
    )
    for (agent_id,) in bind.execute(sa.text("SELECT id FROM agents")):
        exists = bind.scalar(
            sa.text(
                "SELECT agent_id FROM group_agents "
                "WHERE group_id = :group_id AND agent_id = :agent_id"
            ),
            {"group_id": LEGACY_GROUP_ID, "agent_id": agent_id},
        )
        if exists is None:
            bind.execute(
                sa.text(
                    "INSERT INTO group_agents "
                    "(group_id, agent_id, active, max_load_override, created_at, updated_at) "
                    "VALUES (:group_id, :agent_id, :active, NULL, :now, :now)"
                ),
                {
                    "group_id": LEGACY_GROUP_ID,
                    "agent_id": agent_id,
                    "active": True,
                    "now": now,
                },
            )


def downgrade() -> None:
    raise RuntimeError("Migração de escopo não pode ser revertida sem perda de dados")
