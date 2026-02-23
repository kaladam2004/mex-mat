"""
admin.py — Supabase/PostgreSQL version
"""
from fastapi import APIRouter, Depends, HTTPException, Request, Form, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import date, timedelta, datetime
from typing import Optional

from models import (
    User, UserRole, Group, Student, Attendance, Lesson,
    Faculty, Course, AcademicYear, Week, SystemSetting, AuditLog
)
from services import get_password_hash, validate_password_policy, get_system_setting
from dependencies import get_db, get_current_user
from config import templates, SECRET_KEY, ALGORITHM
from jose import jwt

router = APIRouter()
ACCESS_TOKEN_EXPIRE_HOURS = 8


def create_access_token(data: dict) -> str:
    payload = data.copy()
    payload["exp"] = datetime.utcnow() + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    payload["iat"] = datetime.utcnow()
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def _log(db: Session, user_id: int, action: str, table: str, target_id: int, desc: str = ""):
    try:
        db.add(AuditLog(user_id=user_id, action=action, target_table=table,
                        target_id=target_id, description=desc))
        db.flush()
    except Exception:
        pass


# ─── DASHBOARD ────────────────────────────────────────────────────────────────

@router.get("/dashboard", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse("admin.html", {
        "request": request, "user": current_user,
        "current_year": date.today().year, "now": datetime.now(),
    })


# ─── CHANGE PASSWORD ──────────────────────────────────────────────────────────

@router.get("/change-password", response_class=HTMLResponse)
async def change_password_page(request: Request, current_user: User = Depends(get_current_user)):
    return templates.TemplateResponse("change_password.html",
                                      {"request": request, "user": current_user})


@router.post("/change-password", response_class=HTMLResponse)
async def change_password_post(
    request: Request, new_password: str = Form(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    valid, msg = validate_password_policy(new_password)
    if not valid:
        return templates.TemplateResponse("change_password.html",
                                          {"request": request, "user": current_user, "error": msg})
    current_user.password_hash = get_password_hash(new_password)
    current_user.force_password_change = False
    current_user.token_version += 1
    db.commit()
    token = create_access_token({
        "sub": str(current_user.id), "ver": current_user.token_version,
        "role": current_user.role.value,
    })
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(key="access_token", value=f"Bearer {token}",
                        httponly=True, secure=True, samesite="lax", path="/",
                        max_age=ACCESS_TOKEN_EXPIRE_HOURS * 3600)
    return response


# ─── STATS ────────────────────────────────────────────────────────────────────

@router.get("/api/stats")
async def get_stats(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    nb_limit = int(get_system_setting(db, "NB_LIMIT_HIGH", "35"))
    role_counts = {}
    for role in UserRole:
        cnt = db.query(User).filter(User.role == role, User.is_deleted == False).count()
        role_counts[role.value] = cnt
    return {
        "total_users":     db.query(User).filter(User.is_deleted == False).count(),
        "total_faculties": db.query(Faculty).filter(Faculty.is_deleted == False).count(),
        "total_groups":    db.query(Group).filter(Group.is_deleted == False).count(),
        "total_students":  db.query(Student).filter(Student.is_deleted == False).count(),
        "high_absence_students": db.query(Student).filter(
            Student.total_absent_hours >= nb_limit, Student.is_deleted == False
        ).count(),
        "nb_limit": nb_limit,
        "role_counts": role_counts,
    }


# ─── FACULTIES ────────────────────────────────────────────────────────────────

@router.get("/api/faculties")
async def list_faculties(db: Session = Depends(get_db)):
    faculties = db.query(Faculty).filter(Faculty.is_deleted == False).all()
    result = []
    for f in faculties:
        dean = db.query(User).filter(
            User.faculty_id == f.id, User.role == UserRole.DEAN, User.is_deleted == False
        ).first()
        vice = db.query(User).filter(
            User.faculty_id == f.id, User.role == UserRole.VICE_DEAN, User.is_deleted == False
        ).first()
        result.append({
            "id": f.id, "name": f.name, "code": f.code,
            "student_count": db.query(Student).join(Group).filter(
                Group.faculty_id == f.id, Student.is_deleted == False
            ).count(),
            "group_count": db.query(Group).filter(
                Group.faculty_id == f.id, Group.is_deleted == False
            ).count(),
            "dean": {"id": dean.id, "full_name": dean.full_name, "username": dean.username} if dean else None,
            "vice_dean": {"id": vice.id, "full_name": vice.full_name, "username": vice.username} if vice else None,
        })
    return result


@router.post("/api/faculties")
async def create_faculty(
    name: str = Form(...), code: str = Form(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if db.query(Faculty).filter((Faculty.name == name) | (Faculty.code == code)).first():
        raise HTTPException(400, "Факултет бо ин ном ё рамз аллакай мавҷуд аст")
    f = Faculty(name=name, code=code.upper())
    db.add(f)
    db.commit()
    _log(db, current_user.id, "FACULTY_CREATED", "faculties", f.id, name)
    db.commit()
    return {"id": f.id, "name": f.name, "code": f.code}


@router.put("/api/faculties/{fid}")
async def update_faculty(
    fid: int, name: str = Form(...), code: str = Form(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    f = db.query(Faculty).filter(Faculty.id == fid, Faculty.is_deleted == False).first()
    if not f:
        raise HTTPException(404)
    f.name = name; f.code = code.upper()
    db.commit()
    _log(db, current_user.id, "FACULTY_UPDATED", "faculties", fid, name)
    db.commit()
    return {"ok": True}


@router.delete("/api/faculties/{fid}")
async def delete_faculty(
    fid: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    f = db.query(Faculty).filter(Faculty.id == fid).first()
    if not f:
        raise HTTPException(404)
    f.is_deleted = True
    _log(db, current_user.id, "FACULTY_DELETED", "faculties", fid)
    db.commit()
    return {"ok": True}


# ─── USERS ────────────────────────────────────────────────────────────────────

@router.get("/api/users")
async def list_users(
    role: Optional[str] = Query(None),
    faculty_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
):
    q = db.query(User).filter(User.is_deleted == False)
    if role:
        try:
            q = q.filter(User.role == UserRole(role))
        except ValueError:
            pass
    if faculty_id:
        q = q.filter(User.faculty_id == faculty_id)
    users = q.order_by(User.full_name).all()
    result = []
    for u in users:
        group = None
        if u.role == UserRole.CURATOR:
            grp = db.query(Group).filter(
                Group.curator_id == u.id, Group.is_active == True, Group.is_deleted == False
            ).first()
            if grp:
                group = grp.number
        result.append({
            "id": u.id, "full_name": u.full_name, "username": u.username,
            "role": u.role.value, "faculty_id": u.faculty_id,
            "faculty_name": u.faculty.name if u.faculty else None,
            "email": u.email, "phone": u.phone, "department": u.department,
            "birth_year": u.birth_year, "force_password_change": u.force_password_change,
            "group": group,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        })
    return result


@router.post("/api/users")
async def create_user(
    full_name: str = Form(...), username: str = Form(...),
    password: str = Form("020304"), role: str = Form(...),
    faculty_id: Optional[int] = Form(None),
    email: Optional[str] = Form(None), phone: Optional[str] = Form(None),
    department: Optional[str] = Form(None), birth_year: Optional[int] = Form(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if db.query(User).filter(User.username == username, User.is_deleted == False).first():
        raise HTTPException(400, "Ин номи корбар аллакай мавҷуд аст")
    valid, msg = validate_password_policy(password)
    if not valid:
        raise HTTPException(400, msg)
    try:
        user_role = UserRole(role)
    except ValueError:
        raise HTTPException(400, "Нақши нодуруст")
    u = User(
        full_name=full_name, username=username,
        password_hash=get_password_hash(password),
        role=user_role, faculty_id=faculty_id or None,
        email=email, phone=phone, department=department, birth_year=birth_year,
        force_password_change=True, token_version=1,
    )
    db.add(u)
    db.commit()
    _log(db, current_user.id, "USER_CREATED", "users", u.id, f"{full_name} ({role})")
    db.commit()
    return {"id": u.id, "full_name": u.full_name, "username": u.username, "role": u.role.value}


@router.put("/api/users/{uid}")
async def update_user(
    uid: int,
    full_name: Optional[str] = Form(None), email: Optional[str] = Form(None),
    phone: Optional[str] = Form(None), department: Optional[str] = Form(None),
    faculty_id: Optional[int] = Form(None), birth_year: Optional[int] = Form(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    u = db.query(User).filter(User.id == uid, User.is_deleted == False).first()
    if not u:
        raise HTTPException(404, "Корбар ёфт нашуд")
    if full_name: u.full_name = full_name
    if email is not None: u.email = email
    if phone is not None: u.phone = phone
    if department is not None: u.department = department
    if faculty_id is not None: u.faculty_id = faculty_id or None
    if birth_year is not None: u.birth_year = birth_year
    db.commit()
    _log(db, current_user.id, "USER_UPDATED", "users", uid)
    db.commit()
    return {"ok": True}


@router.post("/api/users/{uid}/reset-password")
async def reset_password(
    uid: int, new_password: str = Form("020304"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    u = db.query(User).filter(User.id == uid, User.is_deleted == False).first()
    if not u:
        raise HTTPException(404)
    valid, msg = validate_password_policy(new_password)
    if not valid:
        raise HTTPException(400, msg)
    u.password_hash = get_password_hash(new_password)
    u.force_password_change = True
    u.token_version += 1
    _log(db, current_user.id, "PASSWORD_RESET", "users", uid)
    db.commit()
    return {"ok": True}


@router.delete("/api/users/{uid}")
async def delete_user(
    uid: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if uid == current_user.id:
        raise HTTPException(400, "Худатонро нест карда наметавонед")
    u = db.query(User).filter(User.id == uid, User.is_deleted == False).first()
    if not u:
        raise HTTPException(404)
    u.is_deleted = True
    u.token_version += 1
    _log(db, current_user.id, "USER_DELETED", "users", uid)
    db.commit()
    return {"ok": True}


# ─── GROUPS ───────────────────────────────────────────────────────────────────

@router.get("/api/groups")
async def list_groups(
    faculty_id: Optional[int] = Query(None),
    course_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
):
    q = db.query(Group).filter(Group.is_deleted == False)
    if faculty_id: q = q.filter(Group.faculty_id == faculty_id)
    if course_id:  q = q.filter(Group.course_id == course_id)
    groups = q.order_by(Group.number).all()
    return [{
        "id": g.id, "number": g.number, "shift": g.shift,
        "course_year": g.course.year if g.course else None,
        "course_id": g.course_id,
        "faculty_name": g.faculty.name if g.faculty else None,
        "curator": g.curator.full_name if g.curator else None,
        "curator_id": g.curator_id,
        "total_students": db.query(Student).filter(
            Student.group_id == g.id, Student.is_deleted == False
        ).count(),
        "is_active": g.is_active,
    } for g in groups]


# ─── ACADEMIC YEARS ───────────────────────────────────────────────────────────

@router.get("/api/academic-years")
async def list_academic_years(db: Session = Depends(get_db)):
    years = db.query(AcademicYear).order_by(AcademicYear.start_date.desc()).all()
    return [{"id": y.id, "name": y.name, "start_date": str(y.start_date),
             "end_date": str(y.end_date), "is_current": y.is_current} for y in years]


@router.post("/api/academic-years")
async def create_academic_year(
    name: str = Form(...), start_date: date = Form(...), end_date: date = Form(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    y = AcademicYear(name=name, start_date=start_date, end_date=end_date)
    db.add(y)
    db.commit()
    return {"id": y.id, "name": y.name}


@router.post("/api/academic-years/{yid}/set-current")
async def set_current_year(
    yid: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    db.query(AcademicYear).update({"is_current": False})
    y = db.query(AcademicYear).filter(AcademicYear.id == yid).first()
    if not y:
        raise HTTPException(404)
    y.is_current = True
    db.commit()
    return {"ok": True}


# ─── WEEKS ────────────────────────────────────────────────────────────────────

@router.get("/api/weeks")
async def list_weeks(db: Session = Depends(get_db)):
    weeks = db.query(Week).order_by(Week.start_date.desc()).limit(20).all()
    return [{"id": w.id, "week_number": w.week_number,
             "start_date": str(w.start_date), "end_date": str(w.end_date),
             "is_current": w.is_current} for w in weeks]


@router.post("/api/weeks")
async def create_week(
    academic_year_id: int = Form(...), week_number: int = Form(...),
    start_date: date = Form(...), end_date: date = Form(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    w = Week(academic_year_id=academic_year_id, week_number=week_number,
             start_date=start_date, end_date=end_date)
    db.add(w)
    db.commit()
    return {"id": w.id}


@router.post("/api/weeks/{wid}/set-current")
async def set_current_week(
    wid: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    db.query(Week).update({"is_current": False})
    w = db.query(Week).filter(Week.id == wid).first()
    if not w:
        raise HTTPException(404)
    w.is_current = True
    db.commit()
    return {"ok": True}


# ─── SYSTEM SETTINGS ──────────────────────────────────────────────────────────

@router.get("/api/settings")
async def get_settings(db: Session = Depends(get_db)):
    settings = db.query(SystemSetting).all()
    return [{"id": s.id, "key": s.key, "value": s.value, "description": s.description}
            for s in settings]


@router.put("/api/settings/{key}")
async def update_setting(
    key: str, value: str = Form(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    s = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    if not s:
        s = SystemSetting(key=key, value=value)
        db.add(s)
    else:
        s.value = value
    db.commit()
    return {"ok": True}


# ─── COURSES ──────────────────────────────────────────────────────────────────

@router.get("/api/courses")
async def list_courses(db: Session = Depends(get_db)):
    return [{"id": c.id, "year": c.year} for c in db.query(Course).order_by(Course.year).all()]


# ─── AUDIT LOG ────────────────────────────────────────────────────────────────

@router.get("/api/audit-log")
async def get_audit_log(limit: int = Query(50), db: Session = Depends(get_db)):
    logs = db.query(AuditLog).order_by(AuditLog.timestamp.desc()).limit(limit).all()
    return [{
        "id": l.id, "action": l.action,
        "user": l.user.full_name if l.user else "System",
        "target_table": l.target_table, "target_id": l.target_id,
        "description": l.description,
        "timestamp": l.timestamp.isoformat() if l.timestamp else None,
    } for l in logs]