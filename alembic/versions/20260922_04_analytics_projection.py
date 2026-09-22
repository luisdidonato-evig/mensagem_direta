"""Add analytics outbox and conversation insight projection.

Revision ID: 20260922_04
Revises: 20260922_03
Create Date: 2026-09-22
"""
from typing import Sequence, Union

from alembic import op

from app.models import AnalyticsOutbox, ConversationInsight

revision: str = "20260922_04"
down_revision: Union[str, None] = "20260922_03"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    AnalyticsOutbox.__table__.create(bind=bind, checkfirst=True)
    ConversationInsight.__table__.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    ConversationInsight.__table__.drop(bind=bind, checkfirst=True)
    AnalyticsOutbox.__table__.drop(bind=bind, checkfirst=True)
