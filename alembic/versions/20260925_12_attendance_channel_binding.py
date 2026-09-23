"""Vínculo de canal do atendimento (gateway provider-neutral).

Revision ID: 20260925_12
Revises: 20260924_11
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260925_12"
down_revision: Union[str, None] = "20260924_11"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("attendances")}
    if "channel_account_id" not in columns:
        op.add_column(
            "attendances",
            sa.Column("channel_account_id", sa.String(length=160), nullable=True),
        )
    if "channel_conversation_id" not in columns:
        op.add_column(
            "attendances",
            sa.Column("channel_conversation_id", sa.String(length=160), nullable=True),
        )
        op.create_index(
            "ix_attendances_channel_conversation_id",
            "attendances",
            ["channel_conversation_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("attendances") as batch:
        batch.drop_index("ix_attendances_channel_conversation_id")
        batch.drop_column("channel_conversation_id")
        batch.drop_column("channel_account_id")
