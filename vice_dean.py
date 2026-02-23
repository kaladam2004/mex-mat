"""
vice_dean.py — Supabase/PostgreSQL version
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Form, Query, Body
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import date, timedelta, datetime
from typing import Optional, List, Dict, Any

from models import (
    User, UserRole, Group, Student, Attendance, Lesson,
    Faculty, Course, AuditLog, AcademicYear
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


def get_vd(current_user: User = Depends(get_current_user)) -> User:
    if not current_user or current_user.role != UserRole.VICE_DEAN:
        raise HTTPException(403, "Дастрасӣ нест")
    if not current_user.faculty_id:
        raise HTTPException(403, "Факултет таъин нашудааст")
    return current_user


# ─── DASHBOARD ────────────────────────────────────────────────────────────────

@router.get("/dashboard", response_class=HTMLResponse)
async def vice_dean_dashboard(
    request: Request,
    current_user: User = Depends(get_vd),
    db: Session = Depends(get_db),
):
    faculty   = db.query(Faculty).filter(Faculty.id == current_user.faculty_id).first()
    nb_limit  = int(get_system_setting(db, "NB_LIMIT_HIGH", "35"))
    fid = current_user.faculty_id

    total_students = db.query(Student).join(Group).filter(
        Group.faculty_id == fid, Student.is_deleted == False
    ).count()
    active_groups = db.query(Group).filter(
        Group.faculty_id == fid, Group.is_active == True, Group.is_deleted == False
    ).count()
    high_abs = db.query(Student).join(Group).filter(
        Group.faculty_id == fid,
        Student.total_absent_hours >= nb_limit, Student.is_deleted == False,
    ).count()

    return templates.TemplateResponse("zamdean.html", {
        "request": request, "user": current_user, "faculty": faculty,
        "total_students": total_students, "active_groups": active_groups,
        "high_absence_count": high_abs, "nb_limit": nb_limit,
        "current_year": date.today().year,
    })


# ─── STATS ────────────────────────────────────────────────────────────────────

@router.get("/api/stats/overview")
async def overview_stats(
    current_user: User = Depends(get_vd),
    db: Session = Depends(get_db),
):
    fid = current_user.faculty_id
    nb_limit  = int(get_system_setting(db, "NB_LIMIT_HIGH", "35"))
    cons_days = int(get_system_setting(db, "CONSECUTIVE_ABSENCE_DAYS", "5"))

    total_students = db.query(Student).join(Group).filter(
        Group.faculty_id == fid, Student.is_deleted == False
    ).count()
    total_groups  = db.query(Group).filter(Group.faculty_id == fid, Group.is_deleted == False).count()
    active_groups = db.query(Group).filter(
        Group.faculty_id == fid, Group.is_active == True, Group.is_deleted == False
    ).count()
    total_curators = db.query(User).filter(
        User.faculty_id == fid, User.role == UserRole.CURATOR, User.is_deleted == False
    ).count()
    high_abs = db.query(Student).join(Group).filter(
        Group.faculty_id == fid, Student.total_absent_hours >= nb_limit, Student.is_deleted == False
    ).count()

    courses = db.query(Course).order_by(Course.year).all()
    course_stats = []
    for c in courses:
        gc = db.query(Group).filter(
            Group.faculty_id == fid, Group.course_id == c.id, Group.is_deleted == False
        ).count()
        sc = db.query(Student).join(Group).filter(
            Group.faculty_id == fid, Group.course_id == c.id, Student.is_deleted == False
        ).count()
        if gc > 0:
            course_stats.append({"year": c.year, "groups": gc, "students": sc})

    return {
        "total_students": total_students, "total_groups": total_groups,
        "active_groups": active_groups, "total_curators": total_curators,
        "high_absence_students": high_abs, "nb_limit": nb_limit,
        "consecutive_days": cons_days, "course_stats": course_stats,
    }


# ─── GROUPS ───────────────────────────────────────────────────────────────────

@router.get("/api/groups")
async def list_groups(
    current_user: User = Depends(get_vd),
    db: Session = Depends(get_db),
):
    groups = db.query(Group).filter(
        Group.faculty_id == current_user.faculty_id, Group.is_deleted == False
    ).order_by(Group.number).all()
    return [{
        "id": g.id, "number": g.number, "shift": g.shift,
        "course_year": g.course.year if g.course else None,
        "course_id": g.course_id,
        "curator": g.curator.full_name if g.curator else None,
        "curator_id": g.curator_id,
        "total_students": db.query(Student).filter(
            Student.group_id == g.id, Student.is_deleted == False
        ).count(),
        "is_active": g.is_active,
    } for g in groups]


@router.post("/api/groups")
async def create_group(
    number: str = Form(...), shift: int = Form(...), course_id: int = Form(...),
    curator_id: Optional[int] = Form(None),
    current_user: User = Depends(get_vd),
    db: Session = Depends(get_db),
):
    ay = db.query(AcademicYear).filter(AcademicYear.is_current == True).first()
    if not ay:
        raise HTTPException(400, "Соли таҳсили ҷорӣ муайян нашудааст")
    if curator_id:
        c = db.query(User).filter(
            User.id == curator_id, User.faculty_id == current_user.faculty_id
        ).first()
        if not c:
            raise HTTPException(400, "Куратор ёфт нашуд")
    g = Group(
        number=number, shift=shift, course_id=course_id,
        academic_year_id=ay.id, faculty_id=current_user.faculty_id,
        curator_id=curator_id, is_active=True,
    )
    db.add(g)
    db.commit()
    _log(db, current_user.id, "GROUP_CREATED", "groups", g.id, number)
    return {"id": g.id, "number": g.number}


@router.put("/api/groups/{gid}")
async def update_group(
    gid: int,
    number: str = Form(...), shift: int = Form(...), course_id: int = Form(...),
    curator_id: Optional[int] = Form(None),
    current_user: User = Depends(get_vd),
    db: Session = Depends(get_db),
):
    g = db.query(Group).filter(
        Group.id == gid, Group.faculty_id == current_user.faculty_id, Group.is_deleted == False
    ).first()
    if not g:
        raise HTTPException(404, "Гурӯҳ ёфт нашуд")
    g.number = number; g.shift = shift; g.course_id = course_id; g.curator_id = curator_id
    db.commit()
    _log(db, current_user.id, "GROUP_UPDATED", "groups", gid, number)
    return {"ok": True}


@router.delete("/api/groups/{gid}")
async def delete_group(
    gid: int,
    current_user: User = Depends(get_vd),
    db: Session = Depends(get_db),
):
    g = db.query(Group).filter(
        Group.id == gid, Group.faculty_id == current_user.faculty_id, Group.is_deleted == False
    ).first()
    if not g:
        raise HTTPException(404)
    g.is_deleted = True
    db.commit()
    return {"ok": True}


# ─── STUDENTS ─────────────────────────────────────────────────────────────────

@router.get("/api/students")
async def list_students(
    group_id: Optional[int] = Query(None),
    search: Optional[str] = Query(None),
    course_year: Optional[int] = Query(None),
    high_nb: Optional[bool] = Query(None),
    current_user: User = Depends(get_vd),
    db: Session = Depends(get_db),
):
    fid = current_user.faculty_id
    nb_limit = int(get_system_setting(db, "NB_LIMIT_HIGH", "35"))
    q = db.query(Student).join(Group).filter(Group.faculty_id == fid, Student.is_deleted == False)
    if group_id:
        q = q.filter(Student.group_id == group_id)
    if search and len(search) >= 2:
        q = q.filter(Student.full_name.ilike(f"%{search}%"))
    if course_year:
        q = q.filter(Group.course_id.in_(
            db.query(Course.id).filter(Course.year == course_year)
        ))
    if high_nb:
        q = q.filter(Student.total_absent_hours >= nb_limit)
    students = q.order_by(Student.full_name).all()
    return [{
        "id": s.id, "full_name": s.full_name, "student_code": s.student_code,
        "group_number": s.group.number if s.group else None,
        "course_year": s.group.course.year if s.group and s.group.course else None,
        "total_absences": s.total_absent_hours,
        "birth_place": s.birth_place, "region": s.region, "parent_phone": s.parent_phone,
        "is_high_risk": s.total_absent_hours >= nb_limit,
    } for s in students]


# ─── CURATORS ─────────────────────────────────────────────────────────────────

@router.get("/api/curators")
async def list_curators(
    current_user: User = Depends(get_vd),
    db: Session = Depends(get_db),
):
    curators = db.query(User).filter(
        User.role == UserRole.CURATOR,
        User.faculty_id == current_user.faculty_id,
        User.is_deleted == False,
    ).order_by(User.full_name).all()
    return [{
        "id": c.id, "full_name": c.full_name, "username": c.username,
        "email": c.email, "phone": c.phone, "department": c.department,
        "group": next(({
            "id": g.id, "number": g.number}
            for g in db.query(Group).filter(
                Group.curator_id == c.id, Group.is_deleted == False, Group.is_active == True
            ).all()
        ), None),
    } for c in curators]


@router.post("/api/curators")
async def create_curator(
    full_name: str = Form(...), username: str = Form(...),
    department: Optional[str] = Form(None), email: Optional[str] = Form(None),
    phone: Optional[str] = Form(None),
    current_user: User = Depends(get_vd), db: Session = Depends(get_db),
):
    if db.query(User).filter(User.username == username).first():
        raise HTTPException(400, "Бо ин юзернейм корбар аллакай мавҷуд аст")
    u = User(
        full_name=full_name, username=username, password_hash=get_password_hash("020304"),
        role=UserRole.CURATOR, faculty_id=current_user.faculty_id,
        department=department, email=email, phone=phone,
        force_password_change=True, token_version=1,
    )
    db.add(u)
    db.commit()
    _log(db, current_user.id, "CURATOR_CREATED", "users", u.id, full_name)
    return {"id": u.id, "full_name": u.full_name, "username": u.username}


@router.put("/api/curators/{uid}")
async def update_curator(
    uid: int, full_name: str = Form(...),
    department: Optional[str] = Form(None), email: Optional[str] = Form(None),
    phone: Optional[str] = Form(None),
    current_user: User = Depends(get_vd), db: Session = Depends(get_db),
):
    u = db.query(User).filter(
        User.id == uid, User.role == UserRole.CURATOR,
        User.faculty_id == current_user.faculty_id, User.is_deleted == False,
    ).first()
    if not u:
        raise HTTPException(404, "Куратор ёфт нашуд")
    u.full_name = full_name; u.department = department; u.email = email; u.phone = phone
    db.commit()
    _log(db, current_user.id, "CURATOR_UPDATED", "users", uid, full_name)
    return {"id": u.id, "full_name": u.full_name}


# ─── COURSES ──────────────────────────────────────────────────────────────────

@router.get("/api/courses")
async def list_courses(db: Session = Depends(get_db)):
    return [{"id": c.id, "year": c.year} for c in db.query(Course).order_by(Course.year).all()]


# ─── PROFILE ──────────────────────────────────────────────────────────────────

@router.get("/api/profile")
async def get_profile(current_user: User = Depends(get_vd), db: Session = Depends(get_db)):
    faculty = db.query(Faculty).filter(Faculty.id == current_user.faculty_id).first()
    return {
        "id": current_user.id, "full_name": current_user.full_name,
        "username": current_user.username, "email": current_user.email,
        "phone": current_user.phone, "department": current_user.department,
        "birth_year": current_user.birth_year, "faculty": faculty.name if faculty else None,
    }


@router.put("/api/profile")
async def update_profile(
    full_name: str = Form(...), email: Optional[str] = Form(None),
    phone: Optional[str] = Form(None), department: Optional[str] = Form(None),
    birth_year: Optional[int] = Form(None),
    current_user: User = Depends(get_vd), db: Session = Depends(get_db),
):
    current_user.full_name  = full_name
    current_user.email      = email
    current_user.phone      = phone
    current_user.department = department
    if birth_year:
        current_user.birth_year = birth_year
    db.commit()
    _log(db, current_user.id, "PROFILE_UPDATED", "users", current_user.id, full_name)
    return {"id": current_user.id, "full_name": current_user.full_name}


# ─── CHANGE PASSWORD ──────────────────────────────────────────────────────────

@router.get("/change-password", response_class=HTMLResponse)
async def change_password_page(request: Request, current_user: User = Depends(get_vd)):
    return templates.TemplateResponse("change_password.html",
                                      {"request": request, "user": current_user})


@router.post("/change-password", response_class=HTMLResponse)
async def change_password_post(
    request: Request, new_password: str = Form(...),
    current_user: User = Depends(get_vd), db: Session = Depends(get_db),
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
    response = RedirectResponse("/vice-dean/dashboard", status_code=303)
    response.set_cookie(key="access_token", value=f"Bearer {token}",
                        httponly=True, secure=True, samesite="lax", path="/",
                        max_age=ACCESS_TOKEN_EXPIRE_HOURS * 3600)
    return response