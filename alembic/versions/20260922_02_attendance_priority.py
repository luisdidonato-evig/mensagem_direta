"""Add attendance priority.

Revision ID: 20260922_02
Revises: 20260922_01
Create Date: 2026-09-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260922_02"
down_revision: Union[str, None] = "20260922_01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("attendances")}
    if "priority" not in columns:
        op.add_column(
            "attendances",
            sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        )
        op.create_index("ix_attendances_priority", "attendances", ["priority"])


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("attendances")}
    if "priority" in columns:
        op.drop_index("ix_attendances_priority", table_name="attendances")
        op.drop_column("attendances", "priority")
