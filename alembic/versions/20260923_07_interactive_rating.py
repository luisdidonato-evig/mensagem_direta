"""Interactive messages and customer ratings.

Revision ID: 20260923_07
Revises: 20260922_06
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260923_07"
down_revision: Union[str, None] = "20260922_06"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {item["name"] for item in inspector.get_columns("messages")}
    if "buttons" not in columns:
        op.add_column("messages", sa.Column("buttons", sa.JSON(), nullable=False, server_default="[]"))
    if "interaction_kind" not in columns:
        op.add_column("messages", sa.Column("interaction_kind", sa.String(20), nullable=True))
    if "attendance_ratings" not in inspector.get_table_names():
        op.create_table(
            "attendance_ratings",
            sa.Column("attendance_id", sa.String(36), sa.ForeignKey("attendances.id"), primary_key=True),
            sa.Column("score", sa.Integer(), nullable=False),
            sa.Column("message_id", sa.String(36), sa.ForeignKey("messages.id"), nullable=False, unique=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint("score >= 1 AND score <= 5", name="rating_score_range"),
        )


def downgrade() -> None:
    op.drop_table("attendance_ratings")
    op.drop_column("messages", "interaction_kind")
    op.drop_column("messages", "buttons")
