from datetime import timedelta
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from jose import JWTError, jwt
from pydantic import BaseModel
from sqlalchemy.orm import Session

from automation.database.config import get_db
from automation.database.models import User
from automation.auth.security import (
    verify_password,
    get_password_hash,
    create_access_token,
    create_refresh_token,
    get_current_user,
    ACCESS_TOKEN_EXPIRE_MINUTES,
    ALGORITHM,
    SECRET_KEY,
)

router = APIRouter(prefix="/auth", tags=["auth"])

@router.post("/login")
def login_for_access_token(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == form_data.username).first()
    if not user or not verify_password(form_data.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
        
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user.username}, expires_delta=access_token_expires
    )
    refresh_token = create_refresh_token(data={"sub": user.username})
    
    return {
        "access_token": access_token, 
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "role": user.role
    }

class RefreshIn(BaseModel):
    refresh_token: str


@router.post("/refresh")
def refresh_access_token(body: RefreshIn, db: Session = Depends(get_db)):
    """Exchange a refresh token for a fresh access token.

    Login has always MINTED a refresh token and then thrown it away — there was no
    endpoint to redeem it and the dashboard never stored it. So the 30-minute access
    token was the whole session: the UI silently started 401-ing mid-task and you had
    to sign in again. With this, the session lasts as long as you keep using it and
    ends when you actually log out.

    The refresh token is re-issued on every use (rolling), so continuous use never
    hits the 7-day ceiling, and `type` is checked so an ACCESS token cannot be
    replayed here to mint itself a new one.
    """
    try:
        payload = jwt.decode(body.refresh_token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")
    if payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="Not a refresh token")
    username = payload.get("sub")
    user = db.query(User).filter(User.username == username).first() if username else None
    if not user:
        raise HTTPException(status_code=401, detail="User no longer exists")
    return {
        "access_token": create_access_token(
            data={"sub": user.username},
            expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)),
        "refresh_token": create_refresh_token(data={"sub": user.username}),
        "token_type": "bearer",
        "role": user.role,
    }


@router.get("/me")
def read_users_me(current_user: User = Depends(get_current_user)):
    return {
        "id": current_user.id,
        "username": current_user.username,
        "role": current_user.role
    }

# Helper to easily seed the database with an admin user if it's empty
@router.post("/seed")
def seed_admin(db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == "admin").first()
    if not user:
        hashed_password = get_password_hash("admin")
        new_user = User(username="admin", password_hash=hashed_password, role="admin")
        db.add(new_user)
        db.commit()
        return {"message": "Admin user created (admin/admin)"}
    return {"message": "Admin user already exists"}
