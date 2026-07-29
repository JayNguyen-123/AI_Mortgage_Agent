"""
Login/logout for internal users (loan officers, processors, admins).
Sets an httponly cookie (for the dashboard) and also returns the token
directly (for API/CLI clients that prefer a Bearer header).
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.auth.security import create_access_token, verify_password
from app.db.models import User
from app.db.session import get_db

router = APIRouter(prefix="/auth", tags=["auth"])

COOKIE_MAX_AGE_SECONDS = 60 * 60 * 12  # 12 hours, matches token expiry default


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict


@router.post("/login", response_model=LoginResponse)
def login(req: LoginRequest, response: Response, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == req.email.lower()).first()

    # Constant-shape response whether the email exists or not, so login
    # doesn't leak which emails have accounts.
    if not user or not user.is_active or not verify_password(req.password, user.hashed_password):
        raise HTTPException(401, "Incorrect email or password.")

    token = create_access_token(subject=user.id, role=user.role.value)
    user.last_login_at = datetime.utcnow()
    db.add(user)
    db.commit()

    response.set_cookie(
        key="access_token",
        value=token,
        max_age=COOKIE_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=True,  # requires HTTPS; set False only for plain-HTTP local dev
    )

    return LoginResponse(
        access_token=token,
        user={"id": user.id, "email": user.email, "full_name": user.full_name, "role": user.role.value},
    )


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie("access_token")
    return {"status": "logged_out"}


@router.get("/me")
def me(user: User = Depends(get_current_user)):
    return {"id": user.id, "email": user.email, "full_name": user.full_name, "role": user.role.value}
