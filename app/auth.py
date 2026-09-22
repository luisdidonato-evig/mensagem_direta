from typing import Annotated

import jwt
from fastapi import Header, HTTPException, Request

from app.domain import ActorRole
from app.schemas import Actor
from app.security import JWT_ALGORITHM


def get_actor(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> Actor:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token de acesso ausente")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        payload = jwt.decode(
            token, request.app.state.jwt_secret, algorithms=[JWT_ALGORITHM]
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expirado") from None
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Token inválido") from None

    actor_id = payload.get("sub")
    if not actor_id:
        raise HTTPException(status_code=401, detail="Token sem identificador")
    try:
        role = ActorRole(payload.get("role"))
    except ValueError:
        raise HTTPException(status_code=401, detail="Papel inválido no token") from None
    return Actor(id=actor_id, role=role)
