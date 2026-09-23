"""Handoff IA/humano: modo de automação no atendimento.

Revision ID: 20260924_11
Revises: 20260923_10
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260924_11"
down_revision: Union[str, None] = "20260923_10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("attendances")}
    if "automation_mode" not in columns:
        op.add_column(
            "attendances",
            sa.Column(
                "automation_mode",
                sa.String(length=20),
                nullable=False,
                server_default="AI_ACTIVE",
            ),
        )
        op.create_index("ix_attendances_automation_mode", "attendances", ["automation_mode"])
    if "handoff_reason" not in columns:
        op.add_column("attendances", sa.Column("handoff_reason", sa.String(length=500), nullable=True))
    if "ai_summary" not in columns:
        op.add_column("attendances", sa.Column("ai_summary", sa.Text(), nullable=True))
    if "automation_updated_at" not in columns:
        op.add_column(
            "attendances",
            sa.Column("automation_updated_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    with op.batch_alter_table("attendances") as batch:
        batch.drop_index("ix_attendances_automation_mode")
        batch.drop_column("automation_updated_at")
        batch.drop_column("ai_summary")
        batch.drop_column("handoff_reason")
        batch.drop_column("automation_mode")
