import sys

from app.database import Database
from app.models import Company
from app import provision_tenant


def test_provision_uses_gateway_tenant_id(tmp_path, monkeypatch, capsys):
    tenant_id = "00000000-0000-0000-0000-000000000099"
    database_url = f"sqlite:///{(tmp_path / 'tenant.db').as_posix()}"
    database = Database(database_url)
    database.create_schema()
    database.close()
    monkeypatch.setattr(sys, "argv", [
        "provision_tenant", "--company", "Tenant", "--admin-id", "admin-tenant",
        "--tenant-id", tenant_id, "--database-url", database_url,
    ])
    monkeypatch.setattr(provision_tenant.getpass, "getpass", lambda _: "password-123")
    provision_tenant.main()
    assert f"tenant_id={tenant_id}" in capsys.readouterr().out
    database = Database(database_url)
    with database.session_factory() as session:
        assert session.get(Company, tenant_id).name == "Tenant"
    database.close()
