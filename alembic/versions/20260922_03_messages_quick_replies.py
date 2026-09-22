"""Add sender metadata to messages and scope quick replies.

Revision ID: 20260922_03
Revises: 20260922_02
Create Date: 2026-09-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260922_03"
down_revision: Union[str, None] = "20260922_02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    message_columns = {column["name"] for column in inspector.get_columns("messages")}
    if "sender_type" not in message_columns:
        op.add_column(
            "messages",
            sa.Column(
                "sender_type", sa.String(length=20), nullable=False, server_default="SISTEMA"
            ),
        )
    if "actor_id" not in message_columns:
        op.add_column("messages", sa.Column("actor_id", sa.String(length=120), nullable=True))
        op.create_index("ix_messages_actor_id", "messages", ["actor_id"])

    quick_reply_columns = {
        column["name"] for column in inspector.get_columns("quick_replies")
    }
    if "company_id" not in quick_reply_columns:
        op.add_column(
            "quick_replies", sa.Column("company_id", sa.String(length=36), nullable=True)
        )
        op.create_index("ix_quick_replies_company_id", "quick_replies", ["company_id"])
    if "group_id" not in quick_reply_columns:
        op.add_column(
            "quick_replies", sa.Column("group_id", sa.String(length=36), nullable=True)
        )
        op.create_index("ix_quick_replies_group_id", "quick_replies", ["group_id"])
    if "active" not in quick_reply_columns:
        op.add_column(
            "quick_replies",
            sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        )
        op.create_index("ix_quick_replies_active", "quick_replies", ["active"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    quick_reply_columns = {
        column["name"] for column in inspector.get_columns("quick_replies")
    }
    if "active" in quick_reply_columns:
        op.drop_index("ix_quick_replies_active", table_name="quick_replies")
        op.drop_column("quick_replies", "active")
    if "group_id" in quick_reply_columns:
        op.drop_index("ix_quick_replies_group_id", table_name="quick_replies")
        op.drop_column("quick_replies", "group_id")
    if "company_id" in quick_reply_columns:
        op.drop_index("ix_quick_replies_company_id", table_name="quick_replies")
        op.drop_column("quick_replies", "company_id")

    message_columns = {column["name"] for column in inspector.get_columns("messages")}
    if "actor_id" in message_columns:
        op.drop_index("ix_messages_actor_id", table_name="messages")
        op.drop_column("messages", "actor_id")
    if "sender_type" in message_columns:
        op.drop_column("messages", "sender_type")
