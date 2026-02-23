"""
rector.py — Supabase/PostgreSQL version (READ-ONLY rector)
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Form, Query, Body
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from sqlalchemy import func

from models import (
    User, UserRole, Group, Student, Faculty, Course,
    AuditLog, Attendance, Lesson,
)
from services import get_password_hash, validate_password_policy, get_system_setting
from dependencies import get_db, get_current_user
from config import templates

router = APIRouter(tags=["Rector"])


def get_current_rector(current_user: User = Depends(get_current_user)) -> User:
    if getattr(current_user, "is_deleted", False):
        raise HTTPException(403, "Access denied")
    if current_user.role != UserRole.RECTOR:
        raise HTTPException(403, "Rector role required")
    return current_user


def _audit(db: Session, user_id: int, action: str, table: str,
           target_id: Optional[int] = None, description: str = "") -> None:
    try:
        db.add(AuditLog(user_id=user_id, action=action, target_table=table,
                        target_id=target_id, description=description))
        db.flush()
    except Exception:
        pass


def _week_bounds(offset: int = 0):
    today = date.today()
    mon = today - timedelta(days=today.weekday()) - timedelta(weeks=offset)
    sat = mon + timedelta(days=5)
    return mon, sat


def _att_pct_for_groups(db: Session, group_ids: List[int], target: date):
    if not group_ids:
        return 0.0, 0, 0
    lesson_ids = [lid for (lid,) in db.query(Lesson.id).filter(
        Lesson.group_id.in_(group_ids), Lesson.lesson_date == target
    ).all()]
    if not lesson_ids:
        return 0.0, 0, 0
    counts = dict(db.query(Attendance.status, func.count(Attendance.id)).filter(
        Attendance.lesson_id.in_(lesson_ids)
    ).group_by(Attendance.status).all())
    present  = int(counts.get("present", 0) or 0)
    absent   = int(counts.get("absent", 0) or 0)
    recorded = present + absent
    pct = round(present / recorded * 100.0, 1) if recorded else 0.0
    return pct, present, recorded


# ─── DASHBOARD ────────────────────────────────────────────────────────────────

@router.get("/dashboard", response_class=HTMLResponse)
async def rector_dashboard(
    request: Request,
    current_user: User = Depends(get_current_rector),
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse("rector.html", {
        "request": request, "user": current_user,
    })


# ─── API: OVERVIEW ────────────────────────────────────────────────────────────

@router.get("/api/overview")
async def api_overview(
    current_user: User = Depends(get_current_rector),
    db: Session = Depends(get_db),
):
    today = date.today()
    nb_limit = int(get_system_setting(db, "NB_LIMIT_HIGH", "35"))

    total_faculties = db.query(Faculty).filter(Faculty.is_deleted == False).count()
    total_students  = db.query(Student).filter(Student.is_deleted == False).count()
    total_groups    = db.query(Group).filter(Group.is_deleted == False).count()
    total_staff     = db.query(User).filter(
        User.is_deleted == False, User.role != UserRole.ADMIN
    ).count()
    high_nb = db.query(Student).filter(
        Student.is_deleted == False, Student.total_absent_hours >= nb_limit
    ).count()

    all_group_ids = [gid for (gid,) in db.query(Group.id).filter(Group.is_deleted == False).all()]
    att_today, _, _ = _att_pct_for_groups(db, all_group_ids, today)

    mon0, sat0 = _week_bounds(0)
    mon1, sat1 = _week_bounds(1)

    def _week_att(mon, sat):
        lid = [lid for (lid,) in db.query(Lesson.id).filter(
            Lesson.group_id.in_(all_group_ids),
            Lesson.lesson_date >= mon, Lesson.lesson_date <= sat,
        ).all()]
        if not lid: return 0.0
        counts = dict(db.query(Attendance.status, func.count(Attendance.id)).filter(
            Attendance.lesson_id.in_(lid)
        ).group_by(Attendance.status).all())
        p = int(counts.get("present", 0) or 0)
        a = int(counts.get("absent", 0) or 0)
        t = p + a
        return round(p / t * 100.0, 1) if t else 0.0

    att_this_week = _week_att(mon0, sat0)
    att_prev_week = _week_att(mon1, sat1)

    return {
        "total_faculties": total_faculties,
        "total_students": total_students,
        "total_groups": total_groups,
        "total_staff": total_staff,
        "high_nb_count": high_nb,
        "nb_limit": nb_limit,
        "att_today_pct": att_today,
        "att_this_week_pct": att_this_week,
        "att_prev_week_pct": att_prev_week,
        "dynamics": round(att_this_week - att_prev_week, 1),
    }


# ─── API: FACULTIES ───────────────────────────────────────────────────────────

@router.get("/api/faculties")
async def api_faculties(
    current_user: User = Depends(get_current_rector),
    db: Session = Depends(get_db),
):
    today = date.today()
    nb_limit = int(get_system_setting(db, "NB_LIMIT_HIGH", "35"))
    faculties = db.query(Faculty).filter(Faculty.is_deleted == False).order_by(Faculty.name).all()
    result = []
    for f in faculties:
        group_ids = [gid for (gid,) in db.query(Group.id).filter(
            Group.faculty_id == f.id, Group.is_deleted == False
        ).all()]
        total_students = db.query(Student).filter(
            Student.group_id.in_(group_ids), Student.is_deleted == False
        ).count() if group_ids else 0
        high_nb = db.query(Student).filter(
            Student.group_id.in_(group_ids), Student.is_deleted == False,
            Student.total_absent_hours >= nb_limit,
        ).count() if group_ids else 0
        att_pct, _, _ = _att_pct_for_groups(db, group_ids, today)
        dean = db.query(User).filter(
            User.faculty_id == f.id, User.role == UserRole.DEAN, User.is_deleted == False
        ).first()
        result.append({
            "id": f.id, "name": f.name, "code": f.code,
            "total_students": total_students,
            "total_groups": len(group_ids),
            "high_nb_count": high_nb,
            "att_today_pct": att_pct,
            "dean_name": dean.full_name if dean else None,
        })
    return result


# ─── API: STUDENTS (read-only) ────────────────────────────────────────────────

@router.get("/api/students")
async def api_students(
    faculty_id: Optional[int] = Query(None),
    course_year: Optional[int] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(get_current_rector),
    db: Session = Depends(get_db),
):
    nb_limit = int(get_system_setting(db, "NB_LIMIT_HIGH", "35"))
    q = db.query(Student).join(Group).filter(
        Student.is_deleted == False, Group.is_deleted == False
    )
    if faculty_id:
        q = q.filter(Group.faculty_id == faculty_id)
    if course_year:
        q = q.filter(Group.course_id.in_(
            db.query(Course.id).filter(Course.year == course_year)
        ))
    total    = q.count()
    students = q.order_by(Student.full_name).offset((page - 1) * limit).limit(limit).all()
    fac_map  = {f.id: f.name for f in db.query(Faculty).filter(Faculty.is_deleted == False).all()}

    return {
        "total": total, "page": page, "limit": limit,
        "pages": max(1, (total + limit - 1) // limit),
        "items": [{
            "id": s.id, "full_name": s.full_name, "student_code": s.student_code,
            "group_number": s.group.number if s.group else None,
            "course_year": s.group.course.year if s.group and s.group.course else None,
            "faculty_name": fac_map.get(s.group.faculty_id) if s.group else None,
            "total_absent_hours": s.total_absent_hours,
            "is_high_risk": s.total_absent_hours >= nb_limit,
        } for s in students],
    }


# ─── API: WEEKLY STATS ────────────────────────────────────────────────────────

@router.get("/api/weekly-stats")
async def api_weekly_stats(
    current_user: User = Depends(get_current_rector),
    db: Session = Depends(get_db),
):
    today = date.today()
    nb_limit = int(get_system_setting(db, "NB_LIMIT_HIGH", "35"))
    mon, sat = _week_bounds(0)
    days = [mon + timedelta(days=i) for i in range(6)]

    faculties = db.query(Faculty).filter(Faculty.is_deleted == False).order_by(Faculty.name).all()
    faculty_data = []
    for f in faculties:
        group_ids = [gid for (gid,) in db.query(Group.id).filter(
            Group.faculty_id == f.id, Group.is_deleted == False
        ).all()]
        daily = {}
        for d in days:
            pct, _, _ = _att_pct_for_groups(db, group_ids, d)
            daily[str(d)] = pct
        faculty_data.append({
            "id": f.id, "name": f.name,
            "daily_attendance": daily,
        })

    return {
        "week_start": str(mon), "week_end": str(sat),
        "days": [str(d) for d in days],
        "faculties": faculty_data,
    }


# ─── API: PROFILE ─────────────────────────────────────────────────────────────

@router.get("/api/profile")
async def get_profile(
    current_user: User = Depends(get_current_rector),
    db: Session = Depends(get_db),
):
    return {
        "id": current_user.id, "full_name": current_user.full_name,
        "username": current_user.username, "email": current_user.email,
        "phone": current_user.phone, "birth_year": current_user.birth_year,
        "role": current_user.role.value,
    }


@router.patch("/api/profile")
async def patch_profile(
    payload: Dict[str, Any] = Body(...),
    current_user: User = Depends(get_current_rector),
    db: Session = Depends(get_db),
):
    if "full_name" in payload and payload["full_name"]:
        current_user.full_name = payload["full_name"]
    if "email" in payload:
        current_user.email = payload["email"] or None
    if "phone" in payload:
        current_user.phone = payload["phone"] or None
    _audit(db, current_user.id, "PROFILE_UPDATED", "users", current_user.id)
    db.commit()
    return {"ok": True, "full_name": current_user.full_name}


# ─── CHANGE PASSWORD ──────────────────────────────────────────────────────────

@router.get("/change-password", response_class=HTMLResponse)
async def change_password_page(
    request: Request, current_user: User = Depends(get_current_rector),
):
    return templates.TemplateResponse("change_password.html",
                                      {"request": request, "user": current_user})


@router.post("/change-password", response_class=HTMLResponse)
async def change_password_post(
    request: Request, new_password: str = Form(...),
    current_user: User = Depends(get_current_rector),
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
    return RedirectResponse("/rector/dashboard", status_code=303)