"""Group total capacity and cost per active attendance.

Revision ID: 20260923_09
Revises: 20260923_08
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260923_09"
down_revision: Union[str, None] = "20260923_08"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("service_groups")}
    if "max_active_attendances" not in columns:
        op.add_column("service_groups", sa.Column("max_active_attendances", sa.Integer(), nullable=False, server_default="5"))
        op.execute("UPDATE service_groups SET max_active_attendances = max_load_per_agent")
    if "active_attendance_cost" not in columns:
        op.add_column("service_groups", sa.Column("active_attendance_cost", sa.Numeric(12, 2), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("service_groups", "active_attendance_cost")
    op.drop_column("service_groups", "max_active_attendances")
