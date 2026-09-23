"""Scope contact profiles and audit rows to a company.

Revision ID: 20260923_08
Revises: 20260923_07
"""
from typing import Sequence, Union
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision: str = "20260923_08"
down_revision: Union[str, None] = "20260923_07"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
LEGACY_COMPANY_ID = "00000000-0000-0000-0000-000000000001"


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {item["name"] for item in inspector.get_columns("contacts")}
    if "company_id" not in columns:
        op.add_column("contacts", sa.Column("company_id", sa.String(36), nullable=True))
    if "external_id" not in columns:
        op.add_column("contacts", sa.Column("external_id", sa.String(120), nullable=True))
    audit_columns = {item["name"] for item in inspector.get_columns("contact_audit_events")}
    if "company_id" not in audit_columns:
        op.add_column("contact_audit_events", sa.Column("company_id", sa.String(36), nullable=True))

    bind = op.get_bind()
    contacts = bind.execute(sa.text("SELECT id FROM contacts WHERE external_id IS NULL")).all()
    for (contact_id,) in contacts:
        company_id = bind.scalar(sa.text(
            "SELECT company_id FROM attendances WHERE contact_id = :id AND company_id IS NOT NULL LIMIT 1"
        ), {"id": contact_id}) or LEGACY_COMPANY_ID
        bind.execute(sa.text(
            "UPDATE contacts SET company_id = :company_id, external_id = :id WHERE id = :id"
        ), {"company_id": company_id, "id": contact_id})

    for company_id, contact_id in bind.execute(sa.text(
        "SELECT DISTINCT company_id, contact_id FROM attendances WHERE company_id IS NOT NULL"
    )).all():
        exists = bind.scalar(sa.text(
            "SELECT id FROM contacts WHERE company_id = :company_id AND external_id = :contact_id"
        ), {"company_id": company_id, "contact_id": contact_id})
        if exists is None:
            bind.execute(sa.text(
                "INSERT INTO contacts (id, company_id, external_id, stage, created_at, updated_at) "
                "SELECT :id, :company_id, :external_id, 'NAO_CLASSIFICADO', "
                "MIN(created_at), MAX(updated_at) FROM attendances "
                "WHERE company_id = :company_id AND contact_id = :external_id"
            ), {"id": str(uuid4()), "company_id": company_id, "external_id": contact_id})

    bind.execute(sa.text(
        "UPDATE contact_audit_events SET company_id = COALESCE("
        "(SELECT agents.company_id FROM agents WHERE agents.id = contact_audit_events.actor_id), "
        ":legacy) WHERE company_id IS NULL"
    ), {"legacy": LEGACY_COMPANY_ID})
    bind.execute(sa.text(
        "UPDATE quick_replies SET company_id = COALESCE("
        "(SELECT service_groups.company_id FROM service_groups WHERE service_groups.id = quick_replies.group_id), "
        "(SELECT agents.company_id FROM agents WHERE agents.id = quick_replies.created_by), "
        ":legacy) WHERE company_id IS NULL"
    ), {"legacy": LEGACY_COMPANY_ID})
    contact_indexes = {item["name"] for item in sa.inspect(bind).get_indexes("contacts")}
    if "ix_contacts_company_id" not in contact_indexes:
        op.create_index("ix_contacts_company_id", "contacts", ["company_id"])
    if "ix_contacts_external_id" not in contact_indexes:
        op.create_index("ix_contacts_external_id", "contacts", ["external_id"])
    contact_constraints = {item["name"] for item in sa.inspect(bind).get_unique_constraints("contacts")}
    if "uq_contacts_company_external" not in contact_indexes and "uq_contacts_company_external" not in contact_constraints:
        op.create_index("uq_contacts_company_external", "contacts", ["company_id", "external_id"], unique=True)
    audit_indexes = {item["name"] for item in sa.inspect(bind).get_indexes("contact_audit_events")}
    if "ix_contact_audit_events_company_id" not in audit_indexes:
        op.create_index("ix_contact_audit_events_company_id", "contact_audit_events", ["company_id"])


def downgrade() -> None:
    raise RuntimeError("Não é seguro remover isolamento de contatos por empresa")
