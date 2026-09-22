from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_TTL = timedelta(hours=12)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


def create_access_token(
    actor_id: str, role: str, company_id: str, secret: str
) -> tuple[str, datetime]:
    expires_at = datetime.now(timezone.utc) + ACCESS_TOKEN_TTL
    payload = {"sub": actor_id, "role": role, "company_id": company_id, "exp": expires_at}
    token = jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)
    return token, expires_at
