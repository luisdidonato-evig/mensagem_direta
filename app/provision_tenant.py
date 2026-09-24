"""Provision a company, its first group and administrator.

Run database migrations first. Credentials for Meta stay in META_TENANTS_JSON.
"""
import argparse
import getpass
import os
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from app.database import Database
from app.domain import ActorRole
from app.models import Agent, Company, GroupAgent, ServiceGroup
from app.security import hash_password


def main() -> None:
    parser = argparse.ArgumentParser(description="Criar empresa de atendimento")
    parser.add_argument("--company", required=True, help="Nome da empresa")
    parser.add_argument("--tenant-id", help="UUID do tenant já registrado no gateway")
    parser.add_argument("--admin-id", required=True, help="ID global do administrador")
    parser.add_argument("--group", default="Central", help="Nome do primeiro grupo")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL", "sqlite:///./data/centro_atendimento.db"))
    args = parser.parse_args()
    password = getpass.getpass("Senha do administrador (mínimo 8 caracteres): ")
    if len(password) < 8:
        parser.error("Senha deve ter no mínimo 8 caracteres")
    if not args.company.strip() or not args.group.strip() or not args.admin_id.strip():
        parser.error("Empresa, grupo e ID do administrador são obrigatórios")
    if args.tenant_id:
        try:
            tenant_id = str(UUID(args.tenant_id))
        except ValueError:
            parser.error("tenant-id deve ser UUID válido")
    else:
        tenant_id = None

    database = Database(args.database_url)
    try:
        with database.session_factory() as session:
            company = Company(id=tenant_id, name=args.company.strip()) if tenant_id else Company(name=args.company.strip())
            session.add(company)
            session.flush()
            group = ServiceGroup(company_id=company.id, name=args.group.strip())
            session.add(group)
            session.flush()
            admin = Agent(
                id=args.admin_id.strip(), company_id=company.id,
                role=ActorRole.ADMIN, password_hash=hash_password(password),
            )
            session.add(admin)
            session.add(GroupAgent(group_id=group.id, agent_id=admin.id))
            session.commit()
            print(f"tenant_id={company.id}\ncompany_id={company.id}\ngroup_id={group.id}\nadmin_id={admin.id}")
    except IntegrityError as exc:
        raise SystemExit("ID de administrador já existe ou grupo duplicado") from exc
    finally:
        database.close()


if __name__ == "__main__":
    main()
