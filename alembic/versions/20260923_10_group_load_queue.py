"""Use load units and a configurable waiting message for groups.

Revision ID: 20260923_10
Revises: 20260923_09
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260923_10"
down_revision: Union[str, None] = "20260923_09"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("service_groups")}
    if "load_cost_per_attendance" not in columns:
        op.add_column("service_groups", sa.Column("load_cost_per_attendance", sa.Integer(), nullable=False, server_default="1"))
    if "queue_wait_message" not in columns:
        op.add_column("service_groups", sa.Column("queue_wait_message", sa.Text(), nullable=False,
            server_default="Você entrou na fila de espera para ser atendido."))
    if "active_attendance_cost" in columns:
        op.drop_column("service_groups", "active_attendance_cost")


def downgrade() -> None:
    op.add_column("service_groups", sa.Column("active_attendance_cost", sa.Numeric(12, 2), nullable=False, server_default="0"))
    op.drop_column("service_groups", "queue_wait_message")
    op.drop_column("service_groups", "load_cost_per_attendance")
