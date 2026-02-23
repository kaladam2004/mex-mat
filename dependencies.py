from typing import Generator, Optional

from fastapi import Depends, Request, HTTPException
from sqlalchemy.orm import Session
from jose import jwt, JWTError

from config import SECRET_KEY, ALGORITHM
from models import SessionLocal, User, UserRole


def get_db() -> Generator:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_current_user(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    token = request.cookies.get("access_token")
    if not token:
        return None
    try:
        token = token.replace("Bearer ", "")
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str = payload.get("sub")
        token_version: int = payload.get("ver")
        if not user_id:
            return None
        user = db.query(User).filter(
            User.id == int(user_id),
            User.is_deleted == False
        ).first()
        if not user:
            return None
        if user.token_version != token_version:
            return None
        return user
    except JWTError:
        return None


def require_role(roles: list[UserRole]):
    def checker(current_user: Optional[User] = Depends(get_current_user)):
        if not current_user:
            raise HTTPException(status_code=401, detail="Ворид нашудаед")
        if current_user.role not in roles:
            raise HTTPException(status_code=403, detail="Рухсат нест")
        return current_user
    return checker