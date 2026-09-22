from typing import Annotated

import jwt
from fastapi import Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.domain import ActorRole
from app.models import Agent
from app.schemas import Actor
from app.security import JWT_ALGORITHM


def authenticate_token(token: str, secret: str, session: Session) -> Actor:
    try:
        payload = jwt.decode(token, secret, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expirado") from None
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Token inválido") from None

    actor_id = payload.get("sub")
    company_id = payload.get("company_id")
    if not actor_id:
        raise HTTPException(status_code=401, detail="Token sem identificador")
    if not company_id:
        raise HTTPException(status_code=401, detail="Token sem empresa")
    try:
        role = ActorRole(payload.get("role"))
    except (ValueError, TypeError):
        raise HTTPException(status_code=401, detail="Papel inválido no token") from None
    agent = session.get(Agent, actor_id)
    if agent is None or not agent.active or agent.company_id != company_id or agent.role != role:
        raise HTTPException(status_code=401, detail="Conta ou permissões alteradas")
    return Actor(id=actor_id, role=role, company_id=company_id)


def get_actor(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> Actor:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token de acesso ausente")
    token = authorization.removeprefix("Bearer ").strip()
    with request.app.state.database.session_factory() as session:
        return authenticate_token(token, request.app.state.jwt_secret, session)
