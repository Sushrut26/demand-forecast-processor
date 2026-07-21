"""
Authentication helpers: password hashing, session-cookie identity, and
role-based access guards.

Sessions are signed cookies (itsdangerous) holding only the user id. The
current user is resolved from the database on each request.
"""

from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from itsdangerous import BadSignature, URLSafeSerializer
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import User

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
_serializer = URLSafeSerializer(settings.SECRET_KEY, salt="session")

SESSION_COOKIE = "session"


def hash_password(password: str) -> str:
    return _pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _pwd_context.verify(password, password_hash)
    except ValueError:
        return False


def create_session_value(user_id: int) -> str:
    return _serializer.dumps({"uid": user_id})


def read_session_value(raw: str) -> Optional[int]:
    try:
        data = _serializer.loads(raw)
        return int(data.get("uid"))
    except (BadSignature, ValueError, TypeError):
        return None


def get_current_user(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    """Resolve the logged-in user, or None if not authenticated."""
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    uid = read_session_value(raw)
    if uid is None:
        return None
    user = db.get(User, uid)
    if user is None or not user.is_active:
        return None
    return user


def require_user(user: Optional[User] = Depends(get_current_user)) -> User:
    """Dependency that requires an authenticated, active user."""
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"Location": "/login"},
        )
    return user


def require_admin(user: User = Depends(require_user)) -> User:
    """Dependency that requires an admin user."""
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return user
