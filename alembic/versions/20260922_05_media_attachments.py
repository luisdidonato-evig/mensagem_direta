"""Add private media attachments.

Revision ID: 20260922_05
Revises: 20260922_04
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260922_05"
down_revision: Union[str, None] = "20260922_04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if "media_attachments" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "media_attachments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("message_id", sa.String(36), sa.ForeignKey("messages.id"), nullable=False),
        sa.Column("storage_key", sa.String(36), nullable=False, unique=True),
        sa.Column("mime_type", sa.String(80), nullable=False),
        sa.Column("filename", sa.String(120), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("external_media_id", sa.String(160), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_media_attachments_message_id", "media_attachments", ["message_id"], unique=True)


def downgrade() -> None:
    if "media_attachments" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_index("ix_media_attachments_message_id", table_name="media_attachments")
        op.drop_table("media_attachments")
