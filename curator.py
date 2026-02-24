"""
curator.py — PROFESSIONAL VERSION for Supabase/PostgreSQL
Куратор: daily & weekly attendance, update, only own group
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Form, Query, Body
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import date, timedelta, datetime
from typing import Optional, List, Dict, Any

from models import (
    User, UserRole, Group, Student, Attendance, Lesson,
    Faculty, AuditLog, Course, AcademicYear
)
from services import get_password_hash, validate_password_policy, get_system_setting
from dependencies import get_db, get_current_user
from config import templates, SECRET_KEY, ALGORITHM
from jose import jwt

router = APIRouter()
ACCESS_TOKEN_EXPIRE_HOURS = 8


# ─── HELPERS ──────────────────────────────────────────────────────────────────

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


def _get_group(db: Session, curator_id: int) -> Group:
    """NULL is_active treated as active (backward-compatible with old data)."""
    g = db.query(Group).filter(
        Group.curator_id == curator_id,
        Group.is_deleted == False,
        Group.is_active != False,   # TRUE and NULL both pass; only FALSE is rejected
    ).first()
    if not g:
        raise HTTPException(403, "Гурӯҳи фаъол таъин нашудааст. Ба Декан муроҷиат кунед.")
    return g


def _week_bounds(target: date = None):
    """Return (monday, saturday) for the week that contains `target`."""
    if target is None:
        target = date.today()
    monday = target - timedelta(days=target.weekday())
    saturday = monday + timedelta(days=5)   # 6 working days Mon-Sat
    return monday, saturday


def _recalc_nb(db: Session, student_id: int) -> int:
    total = db.query(
        func.coalesce(func.sum(Attendance.nb_hours), 0)
    ).filter(
        Attendance.student_id == student_id,
        Attendance.status == "absent",
    ).scalar() or 0
    return int(total)


def _get_or_create_lesson(db: Session, group_id: int, lesson_date: date) -> Lesson:
    lesson = db.query(Lesson).filter(
        Lesson.group_id == group_id,
        Lesson.lesson_date == lesson_date,
    ).first()
    if not lesson:
        lesson = Lesson(group_id=group_id, lesson_date=lesson_date, subject="Дарс", lesson_type="lecture")
        db.add(lesson)
        db.flush()
    return lesson


def _upsert_attendance(db: Session, lesson_id: int, student_id: int,
                       nb_hours: int, comment: str, marked_by: int) -> Attendance:
    att = db.query(Attendance).filter(
        Attendance.student_id == student_id,
        Attendance.lesson_id == lesson_id,
    ).first()
    if att:
        att.nb_hours    = nb_hours
        att.status      = "absent" if nb_hours > 0 else "present"
        att.comment     = comment
        att.marked_by   = marked_by
    else:
        att = Attendance(
            student_id=student_id, lesson_id=lesson_id,
            nb_hours=nb_hours,
            status="absent" if nb_hours > 0 else "present",
            comment=comment, marked_by=marked_by,
        )
        db.add(att)
    db.flush()
    return att


# ─── DASHBOARD ────────────────────────────────────────────────────────────────

@router.get("/dashboard", response_class=HTMLResponse)
async def curator_dashboard(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        group = _get_group(db, current_user.id)
    except HTTPException:
        return templates.TemplateResponse("curator.html", {
            "request": request, "user": current_user,
            "error": "Гурӯҳи фаъол таъин нашудааст.",
            "current_year": date.today().year, "group": None,
        })
    faculty = db.query(Faculty).filter(Faculty.id == group.faculty_id).first()
    nb_limit = int(get_system_setting(db, "NB_LIMIT_HIGH", "35"))
    return templates.TemplateResponse("curator.html", {
        "request": request, "user": current_user,
        "group": group, "faculty": faculty,
        "current_year": date.today().year, "nb_limit": nb_limit,
    })


# ─── STATS ────────────────────────────────────────────────────────────────────

@router.get("/api/stats")
async def curator_stats(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        group = _get_group(db, current_user.id)
    except HTTPException:
        return JSONResponse({"error": "no_group"}, status_code=403)

    nb_limit = int(get_system_setting(db, "NB_LIMIT_HIGH", "35"))
    students = db.query(Student).filter(
        Student.group_id == group.id, Student.is_deleted == False
    ).all()
    return {
        "group_number": group.number,
        "shift": group.shift,
        "course_year": group.course.year if group.course else None,
        "total_students": len(students),
        "total_nb_hours": sum(s.total_absent_hours for s in students),
        "high_absence_count": sum(1 for s in students if s.total_absent_hours >= nb_limit),
        "nb_limit": nb_limit,
    }


# ─── STUDENTS ─────────────────────────────────────────────────────────────────

@router.get("/api/students")
async def list_students(
    search: Optional[str] = Query(None),
    birth_place: Optional[str] = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        group = _get_group(db, current_user.id)
    except HTTPException:
        return []

    q = db.query(Student).filter(Student.group_id == group.id, Student.is_deleted == False)
    if search and len(search) >= 2:
        q = q.filter(Student.full_name.ilike(f"%{search}%"))
    if birth_place and len(birth_place) >= 2:
        q = q.filter(Student.birth_place.ilike(f"%{birth_place}%"))

    students = q.order_by(Student.full_name).all()
    nb_limit = int(get_system_setting(db, "NB_LIMIT_HIGH", "35"))
    return [{
        "id": s.id, "full_name": s.full_name, "student_code": s.student_code,
        "birth_year": s.birth_year, "birth_place": s.birth_place,
        "region": s.region, "parent_phone": s.parent_phone,
        "total_absent_hours": s.total_absent_hours,
        "is_high_risk": (s.total_absent_hours or 0) >= nb_limit,
        "study_start": str(s.study_start) if s.study_start else None,
    } for s in students]


@router.get("/api/students/{sid}")
async def get_student(
    sid: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        group = _get_group(db, current_user.id)
    except HTTPException:
        raise HTTPException(403)
    s = db.query(Student).filter(
        Student.id == sid, Student.group_id == group.id, Student.is_deleted == False
    ).first()
    if not s:
        raise HTTPException(404, "Донишҷӯ ёфт нашуд")
    nb_limit = int(get_system_setting(db, "NB_LIMIT_HIGH", "35"))
    return {
        "id": s.id, "full_name": s.full_name, "student_code": s.student_code,
        "birth_year": s.birth_year, "birth_place": s.birth_place,
        "region": s.region, "parent_phone": s.parent_phone,
        "total_absent_hours": s.total_absent_hours,
        "is_high_risk": (s.total_absent_hours or 0) >= nb_limit,
        "study_start": str(s.study_start) if s.study_start else None,
        "expected_graduation": str(s.expected_graduation) if s.expected_graduation else None,
    }


@router.post("/api/students")
async def create_student(
    full_name: str = Form(...),
    birth_year: Optional[int] = Form(None),
    birth_place: Optional[str] = Form(None),
    region: Optional[str] = Form(None),
    parent_phone: Optional[str] = Form(None),
    study_start: Optional[date] = Form(None),
    initial_nb_hours: int = Form(0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        group = _get_group(db, current_user.id)
    except HTTPException:
        raise HTTPException(403, "Гурӯҳи фаъол таъин нашудааст")

    # Generate unique student_code
    last = db.query(Student).order_by(Student.id.desc()).first()
    new_id = (last.id + 1) if last else 1
    student_code = f"STU{new_id:06d}"

    s = Student(
        student_code=student_code, full_name=full_name,
        faculty_id=group.faculty_id, group_id=group.id,
        birth_year=birth_year, birth_place=birth_place, region=region,
        parent_phone=parent_phone, study_start=study_start or date.today(),
        total_absent_hours=initial_nb_hours,
    )
    db.add(s)
    db.commit()
    _log(db, current_user.id, "STUDENT_CREATED", "students", s.id, full_name)
    db.commit()
    return {"id": s.id, "student_code": s.student_code, "full_name": s.full_name}


@router.put("/api/students/{sid}")
async def update_student(
    sid: int,
    full_name: str = Form(...),
    birth_year: Optional[int] = Form(None),
    birth_place: Optional[str] = Form(None),
    region: Optional[str] = Form(None),
    parent_phone: Optional[str] = Form(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        group = _get_group(db, current_user.id)
    except HTTPException:
        raise HTTPException(403)
    s = db.query(Student).filter(
        Student.id == sid, Student.group_id == group.id, Student.is_deleted == False
    ).first()
    if not s:
        raise HTTPException(404, "Донишҷӯ ёфт нашуд")
    s.full_name    = full_name
    s.birth_year   = birth_year
    s.birth_place  = birth_place
    s.region       = region
    s.parent_phone = parent_phone
    db.commit()
    _log(db, current_user.id, "STUDENT_UPDATED", "students", sid, full_name)
    db.commit()
    return {"id": s.id, "full_name": s.full_name}


@router.delete("/api/students/{sid}")
async def delete_student(
    sid: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        group = _get_group(db, current_user.id)
    except HTTPException:
        raise HTTPException(403)
    s = db.query(Student).filter(
        Student.id == sid, Student.group_id == group.id, Student.is_deleted == False
    ).first()
    if not s:
        raise HTTPException(404)
    s.is_deleted = True
    db.commit()
    return {"ok": True}


# ─── JOURNAL: GET WEEK DATA ────────────────────────────────────────────────────

@router.get("/api/journal/week")
async def get_week_journal(
    week_start: Optional[str] = Query(None, description="ISO date of Monday, defaults to current week"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Returns the full weekly journal for the curator's group.
    Each student → attendance per day (Mon–Sat).
    Also returns daily completion status for dean's daily control.
    """
    try:
        group = _get_group(db, current_user.id)
    except HTTPException:
        raise HTTPException(403)

    if week_start:
        try:
            monday = date.fromisoformat(week_start)
        except ValueError:
            raise HTTPException(400, "week_start формати нодуруст (YYYY-MM-DD)")
        # Snap to Monday
        monday = monday - timedelta(days=monday.weekday())
    else:
        monday, _ = _week_bounds()

    saturday = monday + timedelta(days=5)
    days = [monday + timedelta(days=i) for i in range(6)]  # Mon-Sat

    students = db.query(Student).filter(
        Student.group_id == group.id, Student.is_deleted == False
    ).order_by(Student.full_name).all()

    # Load all lessons for this week
    lessons = db.query(Lesson).filter(
        Lesson.group_id == group.id,
        Lesson.lesson_date >= monday,
        Lesson.lesson_date <= saturday,
    ).all()
    lesson_map = {l.lesson_date: l for l in lessons}

    # Load all attendance for this week's lessons
    lesson_ids = [l.id for l in lessons]
    all_att = []
    if lesson_ids:
        all_att = db.query(Attendance).filter(
            Attendance.lesson_id.in_(lesson_ids)
        ).all()

    # att_map: {(student_id, lesson_id) -> Attendance}
    att_map = {(a.student_id, a.lesson_id): a for a in all_att}

    nb_limit = int(get_system_setting(db, "NB_LIMIT_HIGH", "35"))

    student_rows = []
    for s in students:
        days_data = {}
        for d in days:
            lesson = lesson_map.get(d)
            if lesson:
                att = att_map.get((s.id, lesson.id))
                if att:
                    days_data[str(d)] = {
                        "status": att.status,
                        "nb_hours": att.nb_hours,
                        "comment": att.comment or "",
                        "is_reasoned": att.is_reasoned,
                    }
                else:
                    days_data[str(d)] = None  # lesson exists but not marked yet
            else:
                days_data[str(d)] = None  # no lesson for this day
        student_rows.append({
            "id": s.id,
            "full_name": s.full_name,
            "student_code": s.student_code,
            "total_absent_hours": s.total_absent_hours,
            "is_high_risk": (s.total_absent_hours or 0) >= nb_limit,
            "days": days_data,
        })

    # Daily completion status (for dean's view compatibility)
    daily_status = {}
    total_students = len(students)
    for d in days:
        lesson = lesson_map.get(d)
        if not lesson:
            daily_status[str(d)] = {"status": "NOT_STARTED", "marked": 0, "total": total_students}
            continue
        marked = sum(1 for s in students if (s.id, lesson.id) in att_map)
        if marked == 0:
            st = "NOT_STARTED"
        elif marked < total_students:
            st = "IN_PROGRESS"
        else:
            st = "COMPLETED"
        daily_status[str(d)] = {"status": st, "marked": marked, "total": total_students}

    return {
        "group": {"id": group.id, "number": group.number, "shift": group.shift},
        "week_start": str(monday),
        "week_end": str(saturday),
        "days": [str(d) for d in days],
        "students": student_rows,
        "daily_status": daily_status,
        "nb_limit": nb_limit,
        "is_current_week": monday == _week_bounds()[0],
    }


# ─── JOURNAL: MARK SINGLE DAY ─────────────────────────────────────────────────

@router.post("/api/journal/mark-day")
async def mark_day(
    payload: Dict[str, Any] = Body(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Mark attendance for a full day at once.
    payload: {
      "date": "2024-01-15",
      "records": [{"student_id": 1, "nb_hours": 0, "comment": ""}]
    }
    Allowed: current week only.
    """
    try:
        group = _get_group(db, current_user.id)
    except HTTPException:
        raise HTTPException(403)

    mark_date_str = payload.get("date")
    records = payload.get("records", [])

    if not mark_date_str:
        raise HTTPException(400, "Санаи дарс нишон дода нашудааст")

    try:
        mark_date = date.fromisoformat(mark_date_str)
    except ValueError:
        raise HTTPException(400, "Формати санаи нодуруст")

    monday, saturday = _week_bounds()
    if mark_date < monday or mark_date > saturday:
        raise HTTPException(400, "Танҳо ҳафтаи ҷориро таҳрир карда метавонед")

    lesson = _get_or_create_lesson(db, group.id, mark_date)

    # Validate student IDs belong to this group
    valid_sids = {s.id for s in db.query(Student).filter(
        Student.group_id == group.id, Student.is_deleted == False
    ).all()}

    updated = []
    for rec in records:
        sid      = rec.get("student_id")
        nb_hours = int(rec.get("nb_hours", 0))
        comment  = str(rec.get("comment", ""))
        if sid not in valid_sids:
            continue
        if nb_hours < 0 or nb_hours > 8:
            continue
        _upsert_attendance(db, lesson.id, sid, nb_hours, comment, current_user.id)
        updated.append(sid)

    # Recalculate total_absent_hours for updated students
    for sid in updated:
        s = db.query(Student).filter(Student.id == sid).first()
        if s:
            s.total_absent_hours = _recalc_nb(db, sid)

    db.commit()
    return {"ok": True, "date": mark_date_str, "updated_count": len(updated)}


# ─── JOURNAL: MARK SINGLE STUDENT ─────────────────────────────────────────────

@router.post("/api/journal/mark")
async def mark_student(
    payload: Dict[str, Any] = Body(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Mark single student attendance.
    payload: {"student_id": 1, "date": "2024-01-15", "nb_hours": 2, "comment": "..."}
    Allowed: current week only.
    """
    try:
        group = _get_group(db, current_user.id)
    except HTTPException:
        raise HTTPException(403)

    sid          = payload.get("student_id")
    date_str     = payload.get("date")
    nb_hours     = int(payload.get("nb_hours", 0))
    comment      = str(payload.get("comment", ""))

    if not sid or not date_str:
        raise HTTPException(400, "student_id ва date лозим аст")

    try:
        mark_date = date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(400, "Формати санаи нодуруст")

    monday, saturday = _week_bounds()
    if mark_date < monday or mark_date > saturday:
        raise HTTPException(400, "Танҳо ҳафтаи ҷориро таҳрир карда метавонед")

    if nb_hours < 0 or nb_hours > 8:
        raise HTTPException(400, "nb_hours бояд байни 0 ва 8 бошад")

    student = db.query(Student).filter(
        Student.id == sid, Student.group_id == group.id, Student.is_deleted == False
    ).first()
    if not student:
        raise HTTPException(404, "Донишҷӯ ёфт нашуд")

    lesson = _get_or_create_lesson(db, group.id, mark_date)
    _upsert_attendance(db, lesson.id, sid, nb_hours, comment, current_user.id)
    student.total_absent_hours = _recalc_nb(db, sid)
    db.commit()

    return {
        "ok": True, "student_id": sid, "date": date_str,
        "nb_hours": nb_hours, "total_absent_hours": student.total_absent_hours,
    }


# ─── JOURNAL: SAVE FULL WEEK AT ONCE ──────────────────────────────────────────

@router.post("/api/journal/save-week")
async def save_week(
    payload: Dict[str, Any] = Body(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    WEEKLY MODE: Save all attendance for the current week at once.
    payload: {
      "week_start": "2024-01-15",
      "days": {
        "2024-01-15": [{"student_id": 1, "nb_hours": 2, "comment": ""}],
        "2024-01-16": [...]
      }
    }
    """
    try:
        group = _get_group(db, current_user.id)
    except HTTPException:
        raise HTTPException(403)

    week_start_str = payload.get("week_start")
    days_data = payload.get("days", {})

    try:
        monday = date.fromisoformat(week_start_str)
        monday = monday - timedelta(days=monday.weekday())
    except (ValueError, TypeError):
        raise HTTPException(400, "week_start формати нодуруст")

    current_monday, current_saturday = _week_bounds()
    if monday != current_monday:
        raise HTTPException(400, "Танҳо ҳафтаи ҷориро таҳрир карда метавонед")

    saturday = monday + timedelta(days=5)

    valid_sids = {s.id for s in db.query(Student).filter(
        Student.group_id == group.id, Student.is_deleted == False
    ).all()}

    updated_sids = set()
    total_updated = 0

    for date_str, records in days_data.items():
        try:
            d = date.fromisoformat(date_str)
        except ValueError:
            continue
        if d < monday or d > saturday:
            continue

        lesson = _get_or_create_lesson(db, group.id, d)
        for rec in records:
            sid      = rec.get("student_id")
            nb_hours = int(rec.get("nb_hours", 0))
            comment  = str(rec.get("comment", ""))
            if sid not in valid_sids:
                continue
            if nb_hours < 0 or nb_hours > 8:
                continue
            _upsert_attendance(db, lesson.id, sid, nb_hours, comment, current_user.id)
            updated_sids.add(sid)
            total_updated += 1

    # Recalculate totals
    for sid in updated_sids:
        s = db.query(Student).filter(Student.id == sid).first()
        if s:
            s.total_absent_hours = _recalc_nb(db, sid)

    db.commit()
    _log(db, current_user.id, "WEEK_SAVED", "attendance", group.id,
         f"Week {monday} saved, {total_updated} records")
    db.commit()
    return {"ok": True, "week_start": str(monday), "total_updated": total_updated}


# ─── JOURNAL: GET STUDENT HISTORY ─────────────────────────────────────────────

@router.get("/api/journal/student/{sid}")
async def student_attendance_history(
    sid: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Full attendance history for a student."""
    try:
        group = _get_group(db, current_user.id)
    except HTTPException:
        raise HTTPException(403)

    student = db.query(Student).filter(
        Student.id == sid, Student.group_id == group.id, Student.is_deleted == False
    ).first()
    if not student:
        raise HTTPException(404, "Донишҷӯ ёфт нашуд")

    records = (
        db.query(Attendance, Lesson)
        .join(Lesson, Attendance.lesson_id == Lesson.id)
        .filter(Attendance.student_id == sid)
        .order_by(Lesson.lesson_date.desc())
        .all()
    )

    return {
        "student_id": sid,
        "full_name": student.full_name,
        "total_absent_hours": student.total_absent_hours,
        "records": [
            {
                "date": str(l.lesson_date),
                "status": a.status,
                "nb_hours": a.nb_hours,
                "comment": a.comment or "",
                "is_reasoned": a.is_reasoned,
            }
            for a, l in records
        ],
    }


# ─── NB STATS ─────────────────────────────────────────────────────────────────

# Дар curator.py, функсияи nb_stats (~сатр 680-710)

@router.get("/api/nb-stats")
async def nb_stats(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        group = _get_group(db, current_user.id)
    except HTTPException:
        raise HTTPException(403)
    
    nb_limit = int(get_system_setting(db, "NB_LIMIT_HIGH", "35"))
    students = db.query(Student).filter(
        Student.group_id == group.id, Student.is_deleted == False
    ).order_by(Student.total_absent_hours.desc()).all()

    ranges = {"0": 0, "1-10": 0, "11-20": 0, "21-34": 0, "35+": 0}
    
    for s in students:
        # ✅ ИСЛОҲ: None-ро ба 0 табдил диҳед
        h = s.total_absent_hours or 0
        
        if h == 0:
            ranges["0"] += 1
        elif h <= 10:
            ranges["1-10"] += 1
        elif h <= 20:
            ranges["11-20"] += 1
        elif h < nb_limit:
            ranges["21-34"] += 1
        else:
            ranges["35+"] += 1

    return {
        "nb_limit": nb_limit,
        "total_students": len(students),
        "ranges": ranges,
        "high_risk": [
            {"id": s.id, "full_name": s.full_name,
             "total_absent_hours": s.total_absent_hours or 0,
             "parent_phone": s.parent_phone}
            for s in students if (s.total_absent_hours or 0) >= nb_limit
        ],
        "all_students": [
            {"id": s.id, "full_name": s.full_name, 
             "total_absent_hours": s.total_absent_hours or 0}
            for s in students
        ],
    }

# ─── PROFILE ──────────────────────────────────────────────────────────────────

@router.get("/api/profile")
async def get_profile(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    group = db.query(Group).filter(
        Group.curator_id == current_user.id, Group.is_deleted == False
    ).first()
    faculty = db.query(Faculty).filter(Faculty.id == current_user.faculty_id).first() \
        if current_user.faculty_id else None
    return {
        "id": current_user.id, "full_name": current_user.full_name,
        "username": current_user.username, "email": current_user.email,
        "phone": current_user.phone, "department": current_user.department,
        "faculty": faculty.name if faculty else None,
        "group_number": group.number if group else None,
        "birth_year": current_user.birth_year,
        "force_password_change": current_user.force_password_change,
    }


@router.put("/api/profile")
async def update_profile(
    full_name: str = Form(...),
    email: Optional[str] = Form(None),
    phone: Optional[str] = Form(None),
    department: Optional[str] = Form(None),
    birth_year: Optional[int] = Form(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    current_user.full_name  = full_name
    current_user.email      = email
    current_user.phone      = phone
    current_user.department = department
    if birth_year:
        current_user.birth_year = birth_year
    db.commit()
    _log(db, current_user.id, "PROFILE_UPDATED", "users", current_user.id, full_name)
    db.commit()
    return {"id": current_user.id, "full_name": current_user.full_name}


# ─── SUPERVISORS ──────────────────────────────────────────────────────────────

@router.get("/api/supervisors")
async def get_supervisors(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not current_user.faculty_id:
        return []
    result = []
    dean = db.query(User).filter(
        User.role == UserRole.DEAN, User.faculty_id == current_user.faculty_id,
        User.is_deleted == False
    ).first()
    if dean:
        result.append({"role": "Декан", "id": dean.id, "full_name": dean.full_name,
                       "email": dean.email, "phone": dean.phone})
    for vd in db.query(User).filter(
        User.role == UserRole.VICE_DEAN, User.faculty_id == current_user.faculty_id,
        User.is_deleted == False
    ).all():
        result.append({"role": "Замдекан", "id": vd.id, "full_name": vd.full_name,
                       "email": vd.email, "phone": vd.phone})
    rector = db.query(User).filter(User.role == UserRole.RECTOR, User.is_deleted == False).first()
    if rector:
        result.append({"role": "Ректор", "id": rector.id, "full_name": rector.full_name,
                       "email": rector.email, "phone": rector.phone})
    return result


# ─── CHANGE PASSWORD ──────────────────────────────────────────────────────────

@router.get("/change-password", response_class=HTMLResponse)
async def change_password_page(
    request: Request, current_user: User = Depends(get_current_user)
):
    return templates.TemplateResponse("change_password.html",
                                      {"request": request, "user": current_user})


@router.post("/change-password", response_class=HTMLResponse)
async def change_password_post(
    request: Request,
    new_password: str = Form(...),
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
        "sub": str(current_user.id),
        "ver": current_user.token_version,
        "role": current_user.role.value,
    })
    response = RedirectResponse("/curator/dashboard", status_code=303)
    response.set_cookie(key="access_token", value=f"Bearer {token}",
                        httponly=True, secure=True, samesite="lax", path="/",
                        max_age=ACCESS_TOKEN_EXPIRE_HOURS * 3600)
    return response