"""
dean.py — PROFESSIONAL VERSION for Supabase/PostgreSQL
Dean: daily attendance control, weekly view, group/curator management
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Form, Query, Body
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import date, timedelta, datetime
from typing import Optional, List, Dict, Any, Tuple
import csv
import io
import string
import random

from models import (
    User, UserRole, Group, Student, Attendance, Lesson,
    AuditLog, AcademicYear, Course, Faculty, SystemSetting
)
from services import get_password_hash, validate_password_policy, get_system_setting, set_system_setting
from dependencies import get_db, get_current_user
from config import templates

router = APIRouter(tags=["Dean"])


# ─── GUARD ────────────────────────────────────────────────────────────────────

def get_current_dean(current_user: User = Depends(get_current_user)) -> User:
    if getattr(current_user, "is_deleted", False):
        raise HTTPException(403, "Access denied")
    if current_user.role != UserRole.DEAN:
        raise HTTPException(403, "Dean role required")
    if not current_user.faculty_id:
        raise HTTPException(403, "Dean must belong to a faculty")
    return current_user


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def _audit(db: Session, actor_id: int, action: str, table: str,
           target_id: Optional[int], description: str = "") -> None:
    try:
        db.add(AuditLog(user_id=actor_id, action=action, target_table=table,
                        target_id=target_id, description=description))
        db.flush()
    except Exception:
        pass


def _faculty_group_ids(db: Session, faculty_id: int) -> List[int]:
    return [gid for (gid,) in db.query(Group.id).filter(
        Group.faculty_id == faculty_id, Group.is_deleted == False
    ).all()]


def _attendance_pct_for_date(db: Session, faculty_id: int, target: date) -> Tuple[float, int, int]:
    group_ids = _faculty_group_ids(db, faculty_id)
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


def _week_bounds(target: date = None):
    if target is None:
        target = date.today()
    monday = target - timedelta(days=target.weekday())
    saturday = monday + timedelta(days=5)
    return monday, saturday


# ─── DASHBOARD ────────────────────────────────────────────────────────────────

@router.get("/dashboard", response_class=HTMLResponse)
async def dean_dashboard(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_dean),
):
    faculty = db.query(Faculty).filter(Faculty.id == current_user.faculty_id).first()
    return templates.TemplateResponse("dean.html", {
        "request": request, "user": current_user, "faculty": faculty,
    })


# ─── API: OVERVIEW STATS ──────────────────────────────────────────────────────

@router.get("/api/stats/overview")
async def stats_overview(
    current_user: User = Depends(get_current_dean),
    db: Session = Depends(get_db),
):
    fid = current_user.faculty_id
    nb_limit  = int(get_system_setting(db, "NB_LIMIT_HIGH", "35"))
    today = date.today()

    total_students = db.query(Student).join(Group).filter(
        Group.faculty_id == fid, Student.is_deleted == False,
    ).count()
    total_groups = db.query(Group).filter(
        Group.faculty_id == fid, Group.is_deleted == False,
    ).count()
    total_curators = db.query(User).filter(
        User.faculty_id == fid, User.role == UserRole.CURATOR, User.is_deleted == False,
    ).count()
    high_nb_count = db.query(Student).join(Group).filter(
        Group.faculty_id == fid, Student.is_deleted == False,
        Student.total_absent_hours >= nb_limit,
    ).count()
    att_today_pct, present_today, recorded_today = _attendance_pct_for_date(db, fid, today)

    return {
        "total_students": total_students,
        "total_groups": total_groups,
        "total_curators": total_curators,
        "high_nb_count": high_nb_count,
        "nb_limit": nb_limit,
        "attendance_today": {
            "pct": att_today_pct,
            "present": present_today,
            "recorded": recorded_today,
        },
    }


# ─── API: DAILY ATTENDANCE CONTROL ────────────────────────────────────────────

@router.get("/api/daily-control")
async def daily_control(
    target_date: Optional[str] = Query(None, description="YYYY-MM-DD, defaults to today"),
    current_user: User = Depends(get_current_dean),
    db: Session = Depends(get_db),
):
    """
    📊 DAILY CONTROL — for each group show:
    ✅ completed (all students marked)
    ⏳ in_progress (partially marked)
    ❌ not_started (no records yet)
    """
    fid = current_user.faculty_id
    if target_date:
        try:
            d = date.fromisoformat(target_date)
        except ValueError:
            raise HTTPException(400, "Формати санаи нодуруст")
    else:
        d = date.today()

    groups = db.query(Group).filter(
        Group.faculty_id == fid, Group.is_deleted == False, Group.is_active == True
    ).order_by(Group.number).all()

    result = []
    for g in groups:
        total_students = db.query(Student).filter(
            Student.group_id == g.id, Student.is_deleted == False
        ).count()

        lesson = db.query(Lesson).filter(
            Lesson.group_id == g.id, Lesson.lesson_date == d
        ).first()

        if not lesson:
            status = "NOT_STARTED"
            marked = 0
        else:
            marked = db.query(Attendance).filter(
                Attendance.lesson_id == lesson.id
            ).count()
            if marked == 0:
                status = "NOT_STARTED"
            elif marked < total_students:
                status = "IN_PROGRESS"
            else:
                status = "COMPLETED"

        curator = db.query(User).filter(User.id == g.curator_id).first() if g.curator_id else None

        result.append({
            "group_id": g.id,
            "group_number": g.number,
            "shift": g.shift,
            "course_year": g.course.year if g.course else None,
            "curator_name": curator.full_name if curator else None,
            "total_students": total_students,
            "marked": marked,
            "status": status,
            "lesson_id": lesson.id if lesson else None,
        })

    # Summary
    total   = len(result)
    done    = sum(1 for r in result if r["status"] == "COMPLETED")
    partial = sum(1 for r in result if r["status"] == "IN_PROGRESS")
    none    = sum(1 for r in result if r["status"] == "NOT_STARTED")

    return {
        "date": str(d),
        "summary": {
            "total_groups": total,
            "completed": done,
            "in_progress": partial,
            "not_started": none,
        },
        "groups": result,
    }


# ─── API: WEEKLY CONTROL ──────────────────────────────────────────────────────

@router.get("/api/weekly-control")
async def weekly_control(
    week_start: Optional[str] = Query(None, description="ISO date of Monday, defaults to current week"),
    current_user: User = Depends(get_current_dean),
    db: Session = Depends(get_db),
):
    """
    📅 WEEK VIEW — which days were NOT filled in for each group.
    Returns per-group, per-day attendance completion status.
    """
    fid = current_user.faculty_id

    if week_start:
        try:
            monday = date.fromisoformat(week_start)
            monday = monday - timedelta(days=monday.weekday())
        except ValueError:
            raise HTTPException(400, "Формати санаи нодуруст")
    else:
        monday, _ = _week_bounds()

    saturday = monday + timedelta(days=5)
    days = [monday + timedelta(days=i) for i in range(6)]

    groups = db.query(Group).filter(
        Group.faculty_id == fid, Group.is_deleted == False, Group.is_active == True
    ).order_by(Group.number).all()

    # Load all lessons for this week for this faculty's groups
    group_ids = [g.id for g in groups]
    all_lessons = db.query(Lesson).filter(
        Lesson.group_id.in_(group_ids),
        Lesson.lesson_date >= monday,
        Lesson.lesson_date <= saturday,
    ).all() if group_ids else []

    # lesson_map: {(group_id, date) -> lesson}
    lesson_map: Dict[tuple, Lesson] = {}
    for l in all_lessons:
        lesson_map[(l.group_id, l.lesson_date)] = l

    lesson_ids = [l.id for l in all_lessons]
    # Load attendance counts per lesson
    if lesson_ids:
        att_counts_raw = db.query(
            Attendance.lesson_id, func.count(Attendance.id)
        ).filter(Attendance.lesson_id.in_(lesson_ids)).group_by(Attendance.lesson_id).all()
        att_counts = {lid: cnt for lid, cnt in att_counts_raw}
    else:
        att_counts = {}

    result = []
    for g in groups:
        total_students = db.query(Student).filter(
            Student.group_id == g.id, Student.is_deleted == False
        ).count()
        curator = db.query(User).filter(User.id == g.curator_id).first() if g.curator_id else None

        days_status = {}
        missing_days = []
        for d in days:
            lesson = lesson_map.get((g.id, d))
            if not lesson:
                status = "NOT_STARTED"
                marked = 0
            else:
                marked = att_counts.get(lesson.id, 0)
                if marked == 0:
                    status = "NOT_STARTED"
                elif marked < total_students:
                    status = "IN_PROGRESS"
                else:
                    status = "COMPLETED"
            days_status[str(d)] = {
                "status": status,
                "marked": marked,
                "total": total_students,
            }
            if status != "COMPLETED":
                missing_days.append(str(d))

        result.append({
            "group_id": g.id,
            "group_number": g.number,
            "shift": g.shift,
            "course_year": g.course.year if g.course else None,
            "curator_name": curator.full_name if curator else None,
            "total_students": total_students,
            "days": days_status,
            "missing_days": missing_days,
            "completion_pct": round(
                sum(1 for d in days_status.values() if d["status"] == "COMPLETED") / 6 * 100
            ),
        })

    return {
        "week_start": str(monday),
        "week_end": str(saturday),
        "days": [str(d) for d in days],
        "groups": result,
    }


# ─── API: GROUPS ──────────────────────────────────────────────────────────────

@router.get("/api/groups")
async def list_groups(
    current_user: User = Depends(get_current_dean),
    db: Session = Depends(get_db),
):
    fid = current_user.faculty_id
    groups = db.query(Group).filter(
        Group.faculty_id == fid, Group.is_deleted == False
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
    current_user: User = Depends(get_current_dean),
    db: Session = Depends(get_db),
):
    ay = db.query(AcademicYear).filter(AcademicYear.is_current == True).first()
    if not ay:
        raise HTTPException(400, "Соли таҳсили ҷорӣ муайян нашудааст")
    if curator_id:
        c = db.query(User).filter(
            User.id == curator_id, User.faculty_id == current_user.faculty_id,
            User.role == UserRole.CURATOR, User.is_deleted == False
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
    _audit(db, current_user.id, "GROUP_CREATED", "groups", g.id, number)
    db.commit()
    return {"id": g.id, "number": g.number}


@router.put("/api/groups/{gid}")
async def update_group(
    gid: int,
    number: str = Form(...), shift: int = Form(...), course_id: int = Form(...),
    curator_id: Optional[int] = Form(None),
    current_user: User = Depends(get_current_dean),
    db: Session = Depends(get_db),
):
    g = db.query(Group).filter(
        Group.id == gid, Group.faculty_id == current_user.faculty_id, Group.is_deleted == False
    ).first()
    if not g:
        raise HTTPException(404, "Гурӯҳ ёфт нашуд")
    g.number     = number
    g.shift      = shift
    g.course_id  = course_id
    g.curator_id = curator_id
    db.commit()
    _audit(db, current_user.id, "GROUP_UPDATED", "groups", gid, number)
    db.commit()
    return {"ok": True}


@router.patch("/api/groups/{gid}")
async def patch_group(
    gid: int,
    payload: Dict[str, Any] = Body(...),
    current_user: User = Depends(get_current_dean),
    db: Session = Depends(get_db),
):
    g = db.query(Group).filter(
        Group.id == gid, Group.faculty_id == current_user.faculty_id, Group.is_deleted == False
    ).first()
    if not g:
        raise HTTPException(404, "Гурӯҳ ёфт нашуд")
    if "number" in payload and payload["number"]:
        g.number = payload["number"]
    if "shift" in payload and payload["shift"] in (1, 2):
        g.shift = int(payload["shift"])
    if "curator_id" in payload:
        cid_val = payload["curator_id"]
        if cid_val:
            curator = db.query(User).filter(
                User.id == int(cid_val), User.role == UserRole.CURATOR,
                User.faculty_id == current_user.faculty_id, User.is_deleted == False,
            ).first()
            if not curator:
                raise HTTPException(400, "Куратор ёфт нашуд")
            g.curator_id = int(cid_val)
        else:
            g.curator_id = None
    if "is_active" in payload:
        g.is_active = bool(payload["is_active"])
    _audit(db, current_user.id, "GROUP_PATCHED", "groups", gid, str(payload))
    db.commit()
    return {"ok": True}


@router.delete("/api/groups/{gid}")
async def delete_group(
    gid: int,
    current_user: User = Depends(get_current_dean),
    db: Session = Depends(get_db),
):
    g = db.query(Group).filter(
        Group.id == gid, Group.faculty_id == current_user.faculty_id, Group.is_deleted == False
    ).first()
    if not g:
        raise HTTPException(404)
    g.is_deleted = True
    _audit(db, current_user.id, "GROUP_DELETED", "groups", gid)
    db.commit()
    return {"ok": True}


# ─── API: STUDENTS ────────────────────────────────────────────────────────────

@router.get("/api/students")
async def list_students(
    group_id: Optional[int] = Query(None),
    search: Optional[str] = Query(None),
    course_year: Optional[int] = Query(None),
    high_nb: Optional[bool] = Query(None),
    current_user: User = Depends(get_current_dean),
    db: Session = Depends(get_db),
):
    fid = current_user.faculty_id
    nb_limit = int(get_system_setting(db, "NB_LIMIT_HIGH", "35"))
    q = db.query(Student).join(Group).filter(
        Group.faculty_id == fid, Student.is_deleted == False
    )
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
        "birth_place": s.birth_place, "region": s.region,
        "parent_phone": s.parent_phone,
        "is_high_risk": s.total_absent_hours >= nb_limit,
    } for s in students]


@router.get("/api/students/{sid}")
async def get_student(
    sid: int,
    current_user: User = Depends(get_current_dean),
    db: Session = Depends(get_db),
):
    fid = current_user.faculty_id
    s = db.query(Student).join(Group).filter(
        Student.id == sid, Group.faculty_id == fid, Student.is_deleted == False
    ).first()
    if not s:
        raise HTTPException(404, "Донишҷӯ ёфт нашуд")
    nb_limit = int(get_system_setting(db, "NB_LIMIT_HIGH", "35"))
    return {
        "id": s.id, "full_name": s.full_name, "student_code": s.student_code,
        "birth_year": s.birth_year, "birth_place": s.birth_place,
        "region": s.region, "parent_phone": s.parent_phone,
        "study_start": str(s.study_start) if s.study_start else None,
        "total_absent_hours": s.total_absent_hours,
        "group_number": s.group.number if s.group else None,
        "group_id": s.group_id,
        "course_year": s.group.course.year if s.group and s.group.course else None,
        "is_high_risk": s.total_absent_hours >= nb_limit,
    }


@router.get("/api/students/{sid}/attendance")
async def student_attendance(
    sid: int,
    current_user: User = Depends(get_current_dean),
    db: Session = Depends(get_db),
):
    """Full attendance history for a student (read-only for dean)."""
    fid = current_user.faculty_id
    s = db.query(Student).join(Group).filter(
        Student.id == sid, Group.faculty_id == fid, Student.is_deleted == False
    ).first()
    if not s:
        raise HTTPException(404)
    records = (
        db.query(Attendance, Lesson)
        .join(Lesson, Attendance.lesson_id == Lesson.id)
        .filter(Attendance.student_id == sid)
        .order_by(Lesson.lesson_date.desc())
        .all()
    )
    return {
        "student": {"id": s.id, "full_name": s.full_name, "total_absent_hours": s.total_absent_hours},
        "records": [
            {"date": str(l.lesson_date), "status": a.status, "nb_hours": a.nb_hours,
             "comment": a.comment or "", "is_reasoned": a.is_reasoned}
            for a, l in records
        ],
    }


# ─── API: CURATORS ────────────────────────────────────────────────────────────

@router.get("/api/curators")
async def list_curators(
    current_user: User = Depends(get_current_dean),
    db: Session = Depends(get_db),
):
    curators = db.query(User).filter(
        User.role == UserRole.CURATOR,
        User.faculty_id == current_user.faculty_id,
        User.is_deleted == False,
    ).order_by(User.full_name).all()
    result = []
    for c in curators:
        grp = db.query(Group).filter(
            Group.curator_id == c.id, Group.is_deleted == False, Group.is_active == True
        ).first()
        result.append({
            "id": c.id, "full_name": c.full_name, "username": c.username,
            "email": c.email, "phone": c.phone, "department": c.department,
            "group": {"id": grp.id, "number": grp.number} if grp else None,
        })
    return result


@router.post("/api/curators")
async def create_curator(
    full_name: str = Form(...),
    username: str = Form(...),
    department: Optional[str] = Form(None),
    email: Optional[str] = Form(None),
    phone: Optional[str] = Form(None),
    current_user: User = Depends(get_current_dean),
    db: Session = Depends(get_db),
):
    if db.query(User).filter(User.username == username).first():
        raise HTTPException(400, "Бо ин юзернейм корбар аллакай мавҷуд аст")
    u = User(
        full_name=full_name, username=username,
        password_hash=get_password_hash("020304"),
        role=UserRole.CURATOR, faculty_id=current_user.faculty_id,
        department=department, email=email, phone=phone,
        force_password_change=True, token_version=1,
    )
    db.add(u)
    db.commit()
    _audit(db, current_user.id, "CURATOR_CREATED", "users", u.id, full_name)
    db.commit()
    return {"id": u.id, "full_name": u.full_name, "username": u.username}


@router.put("/api/curators/{cid}")
async def update_curator(
    cid: int,
    full_name: str = Form(...),
    department: Optional[str] = Form(None),
    email: Optional[str] = Form(None),
    phone: Optional[str] = Form(None),
    current_user: User = Depends(get_current_dean),
    db: Session = Depends(get_db),
):
    c = db.query(User).filter(
        User.id == cid, User.faculty_id == current_user.faculty_id,
        User.role == UserRole.CURATOR, User.is_deleted == False,
    ).first()
    if not c:
        raise HTTPException(404, "Куратор ёфт нашуд")
    c.full_name  = full_name
    c.department = department
    c.email      = email
    c.phone      = phone
    db.commit()
    _audit(db, current_user.id, "CURATOR_UPDATED", "users", cid, full_name)
    db.commit()
    return {"id": c.id, "full_name": c.full_name}


@router.patch("/api/curators/{cid}")
async def patch_curator(
    cid: int,
    payload: Dict[str, Any] = Body(...),
    current_user: User = Depends(get_current_dean),
    db: Session = Depends(get_db),
):
    c = db.query(User).filter(
        User.id == cid, User.faculty_id == current_user.faculty_id,
        User.role == UserRole.CURATOR, User.is_deleted == False,
    ).first()
    if not c:
        raise HTTPException(404)
    if "full_name" in payload and payload["full_name"]:
        c.full_name = payload["full_name"]
    if "email" in payload:
        c.email = payload["email"] or None
    if "phone" in payload:
        c.phone = payload["phone"] or None
    _audit(db, current_user.id, "CURATOR_PATCHED", "users", cid)
    db.commit()
    return {"ok": True}


@router.post("/api/curators/{cid}/reset-password")
async def reset_curator_password(
    cid: int,
    current_user: User = Depends(get_current_dean),
    db: Session = Depends(get_db),
):
    c = db.query(User).filter(
        User.id == cid, User.faculty_id == current_user.faculty_id,
        User.role == UserRole.CURATOR, User.is_deleted == False,
    ).first()
    if not c:
        raise HTTPException(404)
    c.password_hash = get_password_hash("020304")
    c.force_password_change = True
    c.token_version += 1
    _audit(db, current_user.id, "PASSWORD_RESET", "users", cid, "Dean reset curator password")
    db.commit()
    return {"ok": True, "new_password": "020304"}


@router.delete("/api/curators/{cid}")
async def delete_curator(
    cid: int,
    current_user: User = Depends(get_current_dean),
    db: Session = Depends(get_db),
):
    c = db.query(User).filter(
        User.id == cid, User.faculty_id == current_user.faculty_id,
        User.role == UserRole.CURATOR, User.is_deleted == False,
    ).first()
    if not c:
        raise HTTPException(404)
    c.is_deleted = True
    c.token_version += 1
    _audit(db, current_user.id, "CURATOR_DELETED", "users", cid)
    db.commit()
    return {"ok": True}


# ─── API: COURSES ─────────────────────────────────────────────────────────────

@router.get("/api/courses")
async def list_courses(db: Session = Depends(get_db)):
    return [{"id": c.id, "year": c.year} for c in db.query(Course).order_by(Course.year).all()]


# ─── API: NB / ATTENDANCE STATS ───────────────────────────────────────────────

@router.get("/api/nb-list")
async def nb_list(
    current_user: User = Depends(get_current_dean),
    db: Session = Depends(get_db),
):
    fid = current_user.faculty_id
    nb_limit = int(get_system_setting(db, "NB_LIMIT_HIGH", "35"))
    students = db.query(Student).join(Group).filter(
        Group.faculty_id == fid, Student.is_deleted == False,
        Student.total_absent_hours >= nb_limit,
    ).order_by(Student.total_absent_hours.desc()).all()
    return [{
        "id": s.id, "full_name": s.full_name, "student_code": s.student_code,
        "group_number": s.group.number if s.group else None,
        "total_absent_hours": s.total_absent_hours,
        "parent_phone": s.parent_phone,
    } for s in students]


# ─── API: ATTENDANCE JUSTIFY ──────────────────────────────────────────────────

@router.post("/api/attendance/{att_id}/justify")
async def justify_attendance(
    att_id: int,
    payload: Dict[str, Any] = Body(...),
    current_user: User = Depends(get_current_dean),
    db: Session = Depends(get_db),
):
    """Dean can mark attendance as reasoned/justified."""
    fid = current_user.faculty_id
    att = db.query(Attendance).join(
        Lesson, Attendance.lesson_id == Lesson.id
    ).join(
        Group, Lesson.group_id == Group.id
    ).filter(
        Attendance.id == att_id, Group.faculty_id == fid,
    ).first()
    if not att:
        raise HTTPException(404, "Қайд ёфт нашуд")
    att.is_reasoned = bool(payload.get("is_reasoned", True))
    att.reason_text = payload.get("reason_text", "")
    att.reasoned_by = current_user.id
    db.commit()
    _audit(db, current_user.id, "ATTENDANCE_JUSTIFIED", "attendance", att_id)
    db.commit()
    return {"ok": True}


# ─── API: PROFILE ─────────────────────────────────────────────────────────────

@router.get("/api/profile")
async def get_profile(
    current_user: User = Depends(get_current_dean),
    db: Session = Depends(get_db),
):
    faculty = db.query(Faculty).filter(Faculty.id == current_user.faculty_id).first()
    return {
        "id": current_user.id, "full_name": current_user.full_name,
        "username": current_user.username, "email": current_user.email,
        "phone": current_user.phone, "department": current_user.department,
        "birth_year": current_user.birth_year,
        "faculty": faculty.name if faculty else None,
    }


@router.patch("/api/profile")
async def patch_profile(
    payload: Dict[str, Any] = Body(...),
    current_user: User = Depends(get_current_dean),
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


# ─── API: CHANGE PASSWORD ─────────────────────────────────────────────────────

@router.post("/api/change-password")
async def api_change_password(
    payload: Dict[str, Any] = Body(...),
    current_user: User = Depends(get_current_dean),
    db: Session = Depends(get_db),
):
    from services import verify_password
    current_pw = payload.get("current_password", "")
    new_pw     = payload.get("new_password", "")
    if not verify_password(current_pw, current_user.password_hash):
        raise HTTPException(400, "Пароли ҳозира нодуруст аст")
    valid, msg = validate_password_policy(new_pw)
    if not valid:
        raise HTTPException(400, msg)
    current_user.password_hash = get_password_hash(new_pw)
    current_user.force_password_change = False
    current_user.token_version += 1
    _audit(db, current_user.id, "PASSWORD_CHANGED", "users", current_user.id)
    db.commit()
    return {"ok": True}


@router.get("/change-password", response_class=HTMLResponse)
async def change_password_page(
    request: Request,
    current_user: User = Depends(get_current_dean),
):
    return templates.TemplateResponse("change_password.html",
                                      {"request": request, "user": current_user})


@router.post("/change-password", response_class=HTMLResponse)
async def change_password_post(
    request: Request,
    new_password: str = Form(...),
    current_user: User = Depends(get_current_dean),
    db: Session = Depends(get_db),
):
    valid, msg = validate_password_policy(new_password)
    if not valid:
        return templates.TemplateResponse("change_password.html",
                                          {"request": request, "user": current_user, "error": msg})
    current_user.password_hash = get_password_hash(new_password)
    current_user.force_password_change = False
    current_user.token_version += 1
    _audit(db, current_user.id, "PASSWORD_CHANGED", "users", current_user.id)
    db.commit()
    return RedirectResponse("/dean/dashboard", status_code=303)


# ─── EXPORT ───────────────────────────────────────────────────────────────────

@router.get("/api/export/students")
async def export_students(
    current_user: User = Depends(get_current_dean),
    db: Session = Depends(get_db),
):
    fid = current_user.faculty_id
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["ID", "full_name", "student_code", "group", "course", "total_nb", "region", "parent_phone"])
    for s in db.query(Student).join(Group).filter(
        Group.faculty_id == fid, Student.is_deleted == False
    ).order_by(Student.full_name).all():
        w.writerow([
            s.id, s.full_name, s.student_code,
            s.group.number if s.group else "",
            s.group.course.year if s.group and s.group.course else "",
            int(s.total_absent_hours or 0),
            s.region or "", s.parent_phone or "",
        ])
    return Response(
        content=out.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=students_{date.today()}.csv"},
    )


@router.get("/api/export/nb")
async def export_nb(
    current_user: User = Depends(get_current_dean),
    db: Session = Depends(get_db),
):
    fid = current_user.faculty_id
    nb_limit = int(get_system_setting(db, "NB_LIMIT_HIGH", "35"))
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["ID", "full_name", "group", "course", "total_nb", "parent_phone"])
    for s in db.query(Student).join(Group).filter(
        Group.faculty_id == fid, Student.is_deleted == False,
        Student.total_absent_hours >= nb_limit,
    ).order_by(Student.total_absent_hours.desc()).all():
        w.writerow([
            s.id, s.full_name,
            s.group.number if s.group else "",
            s.group.course.year if s.group and s.group.course else "",
            int(s.total_absent_hours or 0),
            s.parent_phone or "",
        ])
    return Response(
        content=out.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=nb_{date.today()}.csv"},
    )