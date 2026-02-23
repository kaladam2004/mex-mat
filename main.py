"""
main.py — University Management System
Backend: FastAPI + SQLAlchemy + PostgreSQL (Supabase)
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, Depends, Request, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from jose import jwt
from sqlalchemy.orm import Session

from config import SECRET_KEY, ALGORITHM, templates
from models import User, UserRole, LoginHistory, init_db
from dependencies import get_db, get_current_user, require_role
from services import verify_password, validate_password_policy, get_password_hash

import admin, rector, dean, vice_dean, curator

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ACCESS_TOKEN_EXPIRE_HOURS = 8

# ─── APP ──────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="University Management System",
    description="Системаи идоракунии донишгоҳ",
    version="3.0.0",
)

app.mount("/static", StaticFiles(directory="static"), name="static")

app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── TOKEN ────────────────────────────────────────────────────────────────────

def create_access_token(data: dict) -> str:
    payload = data.copy()
    payload["exp"] = datetime.utcnow() + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    payload["iat"] = datetime.utcnow()
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


# ─── EXCEPTION HANDLERS ───────────────────────────────────────────────────────

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code in (302, 303) and "Location" in exc.headers:
        return RedirectResponse(url=exc.headers["Location"], status_code=303)
    if exc.status_code == 401:
        return templates.TemplateResponse("login.html", {
            "request": request, "error": "Ворид нашудаед. Лутфан ворид шавед."
        }, status_code=401)
    if exc.status_code == 403:
        return templates.TemplateResponse("login.html", {
            "request": request, "error": "Рухсат нест."
        }, status_code=403)
    if exc.status_code == 404:
        return JSONResponse({"detail": "Ёфт нашуд"}, status_code=404)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


# ─── HOME ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def home(current_user: Optional[User] = Depends(get_current_user)):
    if not current_user:
        return RedirectResponse("/login", status_code=303)

    # Force password change redirect
    if current_user.force_password_change:
        role_prefix = {
            UserRole.ADMIN:     "/admin",
            UserRole.RECTOR:    "/rector",
            UserRole.DEAN:      "/dean",
            UserRole.VICE_DEAN: "/vice-dean",
            UserRole.CURATOR:   "/curator",
        }
        prefix = role_prefix.get(current_user.role, "")
        return RedirectResponse(f"{prefix}/change-password", status_code=303)

    role_map = {
        UserRole.ADMIN:     "/admin/dashboard",
        UserRole.RECTOR:    "/rector/dashboard",
        UserRole.DEAN:      "/dean/dashboard",
        UserRole.VICE_DEAN: "/vice-dean/dashboard",
        UserRole.CURATOR:   "/curator/dashboard",
    }
    return RedirectResponse(role_map.get(current_user.role, "/login"), status_code=303)


# ─── LOGIN ────────────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, current_user: Optional[User] = Depends(get_current_user)):
    if current_user and not current_user.force_password_change:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login")
def login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(
        User.username == username, User.is_deleted == False
    ).first()

    if not user or not verify_password(password, user.password_hash):
        db.add(LoginHistory(
            user_id=user.id if user else None,
            ip_address=request.client.host if request.client else None,
            success=False,
        ))
        db.commit()
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "Номи корбар ё парол нодуруст аст",
        })

    db.add(LoginHistory(
        user_id=user.id,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent", ""),
        success=True,
    ))
    db.commit()

    token = create_access_token({
        "sub": str(user.id),
        "ver": user.token_version,
        "role": user.role.value,
    })
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        key="access_token", value=f"Bearer {token}",
        httponly=True, secure=True, samesite="lax", path="/",
        max_age=ACCESS_TOKEN_EXPIRE_HOURS * 3600,
    )
    return response


# ─── LOGOUT ───────────────────────────────────────────────────────────────────

@app.get("/logout")
def logout(
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user:
        current_user.token_version += 1
        db.commit()
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("access_token", path="/")
    return response


# ─── GLOBAL CHANGE PASSWORD ───────────────────────────────────────────────────

@app.get("/change-password", response_class=HTMLResponse)
def global_change_password_page(
    request: Request, current_user: Optional[User] = Depends(get_current_user)
):
    if not current_user:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse("change_password.html",
                                      {"request": request, "user": current_user})


@app.post("/change-password", response_class=HTMLResponse)
def global_change_password_post(
    request: Request,
    new_password: str = Form(...),
    current_user: Optional[User] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not current_user:
        return RedirectResponse("/login", status_code=303)
    valid, msg = validate_password_policy(new_password)
    if not valid:
        return templates.TemplateResponse("change_password.html", {
            "request": request, "user": current_user, "error": msg,
        })
    current_user.password_hash = get_password_hash(new_password)
    current_user.force_password_change = False
    current_user.token_version += 1
    db.commit()
    token = create_access_token({
        "sub": str(current_user.id),
        "ver": current_user.token_version,
        "role": current_user.role.value,
    })
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        key="access_token", value=f"Bearer {token}",
        httponly=True, secure=True, samesite="lax", path="/",
        max_age=ACCESS_TOKEN_EXPIRE_HOURS * 3600,
    )
    return response


# ─── ROUTERS ──────────────────────────────────────────────────────────────────

app.include_router(admin.router,     prefix="/admin",
                   dependencies=[Depends(require_role([UserRole.ADMIN]))])
app.include_router(rector.router,    prefix="/rector",
                   dependencies=[Depends(require_role([UserRole.RECTOR]))])
app.include_router(dean.router,      prefix="/dean",
                   dependencies=[Depends(require_role([UserRole.DEAN]))])
app.include_router(vice_dean.router, prefix="/vice-dean",
                   dependencies=[Depends(require_role([UserRole.VICE_DEAN]))])
app.include_router(curator.router,   prefix="/curator",
                   dependencies=[Depends(require_role([UserRole.CURATOR]))])


# ─── STARTUP ──────────────────────────────────────────────────────────────────

@app.on_event("startup")
def startup():
    init_db()
    logger.info("✅ University Management System v3.0 started (Supabase/PostgreSQL)")


# ─── HEALTH ───────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "version": "3.0.0", "db": "supabase"}