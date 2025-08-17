from fastapi import Depends, HTTPException, status, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Dict, Any, List, Callable
from backend.common.security import verify_access_token
from pydantic import BaseModel

bearer = HTTPBearer(auto_error=False)

class User(BaseModel):
    sub: str
    roles: List[str] = []

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(bearer)) -> User:
    if not credentials or not credentials.credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing or invalid credentials")
    token = credentials.credentials
    try:
        payload = verify_access_token(token)
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    roles = payload.get("roles", [])
    return User(sub=payload.get("sub"), roles=roles)

def requires_roles(required: List[str]):
    def decorator(func: Callable):
        async def wrapper(*args, user: User = Depends(get_current_user), **kwargs):
            # simple check: any intersection
            if not set(required).intersection(set(user.roles)):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")
            return await func(*args, **kwargs, user=user)
        return wrapper
    return decorator
