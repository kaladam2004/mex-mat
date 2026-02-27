"""
curator.py — SENIOR REFACTOR v4.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Ислоҳоти асосӣ:
  1. NB ҲЕҶ ГОҲ DELETE НАМЕШАВАД — танҳо upsert (add/update)
  2. Гурӯҳи маҳкамшуда (is_closed=True) — ҳеҷ амали навис иҷозат нест
  3. Давомоти рӯзона: mark_date метавонад ҳар рӯзи ҳафтаи ҷорӣ бошад
  4. Давомоти ҳафтаина: танҳо Шанбе (weekday==5) фаъол
  5. Profile: танҳо полеҳои дар модел мавҷудбуда (study_lang/education_type нест)
  6. update_profile: JSON body (на Form) барои ҳамоҳангӣ бо frontend
  7. Дастрасӣ ба профили донишҷӯён ва роҳбарият
"""
from __future__ import annotations

from datetime import date, timedelta, datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jose import jwt
from sqlalchemy import func
from sqlalchemy.orm import Session

from config import ALGORITHM, SECRET_KEY, templates
from dependencies import get_current_user, get_db
from models import (
    AcademicYear,
    AuditLog,
    Attendance,
    Course,
    Faculty,
    Group,
    Lesson,
    Student,
    User,
    UserRole,
)
from services import get_password_hash, get_system_setting, validate_password_policy

router = APIRouter()
ACCESS_TOKEN_EXPIRE_HOURS = 8


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def create_access_token(data: dict) -> str:
    payload = data.copy()
    payload["exp"] = datetime.utcnow() + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    payload["iat"] = datetime.utcnow()
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def _log(
    db: Session,
    user_id: int,
    action: str,
    table: str,
    target_id: int,
    desc: str = "",
):
    try:
        db.add(
            AuditLog(
                user_id=user_id,
                action=action,
                target_table=table,
                target_id=target_id,
                description=desc,
            )
        )
        db.flush()
    except Exception:
        pass


def _get_group(db: Session, curator_id: int) -> Group:
    """
    Гурӯҳи фаъол ва кушода.
    NULL is_active — фаъол ҳисоб мешавад (backward-compatible).
    is_closed=True — куратор наметавонад амале иҷро кунад.
    """
    g = db.query(Group).filter(
        Group.curator_id == curator_id,
        Group.is_deleted == False,
        Group.is_active != False,
    ).first()
    if not g:
        raise HTTPException(
            403, "Гурӯҳи фаъол таъин нашудааст. Ба Декан муроҷиат кунед."
        )
    return g


def _get_open_group(db: Session, curator_id: int) -> Group:
    """
    Гурӯҳи фаъол ва кушода барои амалҳои навис.
    Агар гурӯҳ маҳкам шуда бошад — 403 баргардонида мешавад.
    """
    g = _get_group(db, curator_id)
    if g.is_closed:
        raise HTTPException(
            403,
            "Гурӯҳ маҳкам шудааст. Тағйир додан мумкин нест.",
        )
    return g


def _week_bounds(target: Optional[date] = None):
    """Душанбе ва Шанбеи ҳафтаи дорои target."""
    if target is None:
        target = date.today()
    monday = target - timedelta(days=target.weekday())
    saturday = monday + timedelta(days=5)
    return monday, saturday


def _recalc_nb(db: Session, student_id: int) -> int:
    """
    Ҳамаи nb_hours-и absent-ҳоро ҷамъ мекунад.
    ҲЕҶ ГОҲ DELETE намешавад — танҳо recalc аз базаи мавҷуда.
    """
    total = (
        db.query(func.coalesce(func.sum(Attendance.nb_hours), 0))
        .filter(Attendance.student_id == student_id, Attendance.status == "absent")
        .scalar()
        or 0
    )
    return int(total)


def _get_or_create_lesson(db: Session, group_id: int, lesson_date: date) -> Lesson:
    lesson = db.query(Lesson).filter(
        Lesson.group_id == group_id,
        Lesson.lesson_date == lesson_date,
    ).first()
    if not lesson:
        lesson = Lesson(
            group_id=group_id,
            lesson_date=lesson_date,
            subject="Дарс",
            lesson_type="lecture",
        )
        db.add(lesson)
        db.flush()
    return lesson


def _upsert_attendance(
    db: Session,
    lesson_id: int,
    student_id: int,
    nb_hours: int,
    comment: str,
    marked_by: int,
) -> Attendance:
    """
    UPSERT — ҲЕЧ ГОЗ DELETE НАМЕКУНАД.
    Агар record мавҷуд бошад → UPDATE, агар не → INSERT.
    """
    att = db.query(Attendance).filter(
        Attendance.student_id == student_id,
        Attendance.lesson_id == lesson_id,
    ).first()
    if att:
        att.nb_hours = nb_hours
        att.status = "absent" if nb_hours > 0 else "present"
        att.comment = comment
        att.marked_by = marked_by
    else:
        att = Attendance(
            student_id=student_id,
            lesson_id=lesson_id,
            nb_hours=nb_hours,
            status="absent" if nb_hours > 0 else "present",
            comment=comment,
            marked_by=marked_by,
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
        return templates.TemplateResponse(
            "curator.html",
            {
                "request": request,
                "user": current_user,
                "error": "Гурӯҳи фаъол таъин нашудааст.",
                "current_year": date.today().year,
                "group": None,
            },
        )
    faculty = db.query(Faculty).filter(Faculty.id == group.faculty_id).first()
    nb_limit = int(get_system_setting(db, "NB_LIMIT_HIGH", "35"))
    return templates.TemplateResponse(
        "curator.html",
        {
            "request": request,
            "user": current_user,
            "group": group,
            "faculty": faculty,
            "current_year": date.today().year,
            "nb_limit": nb_limit,
        },
    )


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
    course_year = group.course.year if group.course else None
    academic_year = (
        db.query(AcademicYear).filter(AcademicYear.id == group.academic_year_id).first()
        if group.academic_year_id
        else None
    )
    faculty = (
        db.query(Faculty).filter(Faculty.id == group.faculty_id).first()
        if group.faculty_id
        else None
    )

    return {
        "group_id": group.id,
        "group_number": group.number,
        "shift": group.shift,
        "is_closed": bool(group.is_closed),
        "is_active": group.is_active,
        "course_year": course_year,
        "academic_year": academic_year.name if academic_year else None,
        "faculty": faculty.name if faculty else None,
        "faculty_code": faculty.code if faculty else None,
        "total_students": len(students),
        "total_nb_hours": sum((s.total_absent_hours or 0) for s in students),
        "high_absence_count": sum(
            1 for s in students if (s.total_absent_hours or 0) >= nb_limit
        ),
        "nb_limit": nb_limit,
        "created_at": group.created_at.isoformat() if group.created_at else None,
        "updated_at": group.updated_at.isoformat() if group.updated_at else None,
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

    nb_limit = int(get_system_setting(db, "NB_LIMIT_HIGH", "35"))
    q = db.query(Student).filter(
        Student.group_id == group.id, Student.is_deleted == False
    )
    if search and len(search) >= 2:
        q = q.filter(Student.full_name.ilike(f"%{search}%"))
    if birth_place and len(birth_place) >= 2:
        q = q.filter(Student.birth_place.ilike(f"%{birth_place}%"))

    students = q.order_by(Student.full_name).all()
    return [
        {
            "id": s.id,
            "full_name": s.full_name,
            "student_code": s.student_code,
            "birth_year": s.birth_year,
            "birth_place": s.birth_place or "",
            "region": s.region or "",
            "parent_phone": s.parent_phone or "",
            "study_start": str(s.study_start) if s.study_start else None,
            "expected_graduation": str(s.expected_graduation) if s.expected_graduation else None,
            "group_shift": group.shift,
            "total_absent_hours": s.total_absent_hours or 0,
            "is_high_risk": (s.total_absent_hours or 0) >= nb_limit,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "updated_at": s.updated_at.isoformat() if s.updated_at else None,
        }
        for s in students
    ]


@router.get("/api/students/{sid}")
async def get_student(
    sid: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Профили пурраи донишҷӯ бо таърихи ҳамаи NB-ҳо ва санаи дақиқ."""
    try:
        group = _get_group(db, current_user.id)
    except HTTPException:
        raise HTTPException(403)

    s = db.query(Student).filter(
        Student.id == sid,
        Student.group_id == group.id,
        Student.is_deleted == False,
    ).first()
    if not s:
        raise HTTPException(404, "Донишҷӯ ёфт нашуд")

    # Ҳамаи attendance-ҳо барои таърихи пурра
    records = (
        db.query(Attendance, Lesson)
        .join(Lesson, Attendance.lesson_id == Lesson.id)
        .filter(Attendance.student_id == sid)
        .order_by(Lesson.lesson_date.desc())
        .all()
    )

    nb_limit = int(get_system_setting(db, "NB_LIMIT_HIGH", "35"))
    return {
        # ── Identity ──────────────────────────────────────────────────────
        "id": s.id,
        "full_name": s.full_name,
        "student_code": s.student_code,
        # ── Group ─────────────────────────────────────────────────────────
        "group_number": group.number,
        "group_shift": group.shift,
        "group_id": s.group_id,
        # ── Personal ──────────────────────────────────────────────────────
        "birth_year": s.birth_year,
        "birth_place": s.birth_place or "",
        "region": s.region or "",
        "parent_phone": s.parent_phone or "",
        # ── Education ─────────────────────────────────────────────────────
        "study_start": str(s.study_start) if s.study_start else None,
        "expected_graduation": str(s.expected_graduation) if s.expected_graduation else None,
        # ── Attendance ────────────────────────────────────────────────────
        "total_absent_hours": s.total_absent_hours or 0,
        "is_high_risk": (s.total_absent_hours or 0) >= nb_limit,
        "nb_limit": nb_limit,
        # ── NB History (танҳо absent, бо санаи дақиқ) ────────────────────
        "nb_history": [
            {
                "date": str(l.lesson_date),
                "nb_hours": a.nb_hours,
                "status": a.status,
                "comment": a.comment or "",
                "is_reasoned": a.is_reasoned,
                "reason_text": a.reason_text or "",
            }
            for a, l in records
            if (a.nb_hours or 0) > 0
        ],
        # ── Timestamps ────────────────────────────────────────────────────
        "is_deleted": s.is_deleted,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


@router.post("/api/students")
async def create_student(
    payload: Dict[str, Any] = Body(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Донишҷӯи нав илова кардан.
    Агар initial_nb_hours > 0 бошад → attendance record барои NB-и пешина.
    JSON body барои ҳамоҳангӣ бо frontend fetch().
    """
    try:
        group = _get_open_group(db, current_user.id)
    except HTTPException:
        raise

    full_name = (payload.get("full_name") or "").strip()
    if not full_name:
        raise HTTPException(400, "Ному насаб лозим аст")

    birth_year = payload.get("birth_year") or None
    birth_place = payload.get("birth_place") or None
    region = payload.get("region") or None
    parent_phone = payload.get("parent_phone") or None
    study_start_str = payload.get("study_start") or None
    initial_nb_hours = int(payload.get("initial_nb_hours", 0) or 0)

    study_start = None
    if study_start_str:
        try:
            study_start = date.fromisoformat(study_start_str)
        except ValueError:
            pass

    # Генератори рамзи донишҷӯ
    last = db.query(Student).order_by(Student.id.desc()).first()
    new_id = (last.id + 1) if last else 1
    student_code = f"STU{new_id:06d}"

    s = Student(
        student_code=student_code,
        full_name=full_name,
        faculty_id=group.faculty_id,
        group_id=group.id,
        birth_year=int(birth_year) if birth_year else None,
        birth_place=birth_place,
        region=region,
        parent_phone=parent_phone,
        study_start=study_start or date.today(),
        total_absent_hours=initial_nb_hours,
    )
    db.add(s)
    db.flush()  # get s.id

    # NB-и пешина (аз гурӯҳи дигар) → attendance record
    if initial_nb_hours > 0:
        transfer_lesson = _get_or_create_lesson(db, group.id, date.today())
        existing = db.query(Attendance).filter(
            Attendance.student_id == s.id,
            Attendance.lesson_id == transfer_lesson.id,
        ).first()
        if not existing:
            db.add(
                Attendance(
                    student_id=s.id,
                    lesson_id=transfer_lesson.id,
                    nb_hours=initial_nb_hours,
                    status="absent",
                    comment="НБ-и пешина (кӯч аз гурӯҳи дигар)",
                    marked_by=current_user.id,
                )
            )

    db.commit()
    _log(db, current_user.id, "STUDENT_CREATED", "students", s.id, full_name)
    db.commit()
    return {"id": s.id, "student_code": s.student_code, "full_name": s.full_name}


@router.put("/api/students/{sid}")
async def update_student(
    sid: int,
    payload: Dict[str, Any] = Body(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        group = _get_open_group(db, current_user.id)
    except HTTPException:
        raise
    s = db.query(Student).filter(
        Student.id == sid,
        Student.group_id == group.id,
        Student.is_deleted == False,
    ).first()
    if not s:
        raise HTTPException(404, "Донишҷӯ ёфт нашуд")

    if "full_name" in payload and payload["full_name"]:
        s.full_name = str(payload["full_name"]).strip()
    if "birth_year" in payload:
        s.birth_year = int(payload["birth_year"]) if payload["birth_year"] else None
    if "birth_place" in payload:
        s.birth_place = payload["birth_place"] or None
    if "region" in payload:
        s.region = payload["region"] or None
    if "parent_phone" in payload:
        s.parent_phone = payload["parent_phone"] or None

    db.commit()
    _log(db, current_user.id, "STUDENT_UPDATED", "students", sid, s.full_name)
    db.commit()
    return {"id": s.id, "full_name": s.full_name}


@router.delete("/api/students/{sid}")
async def delete_student(
    sid: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        group = _get_open_group(db, current_user.id)
    except HTTPException:
        raise
    s = db.query(Student).filter(
        Student.id == sid,
        Student.group_id == group.id,
        Student.is_deleted == False,
    ).first()
    if not s:
        raise HTTPException(404)
    s.is_deleted = True
    db.commit()
    return {"ok": True}


# ─── JOURNAL: GET WEEK DATA ────────────────────────────────────────────────────

@router.get("/api/journal/week")
async def get_week_journal(
    week_start: Optional[str] = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        group = _get_group(db, current_user.id)
    except HTTPException:
        raise HTTPException(403)

    if week_start:
        try:
            monday = date.fromisoformat(week_start)
        except ValueError:
            raise HTTPException(400, "week_start формати нодуруст (YYYY-MM-DD)")
        monday = monday - timedelta(days=monday.weekday())
    else:
        monday, _ = _week_bounds()

    saturday = monday + timedelta(days=5)
    days = [monday + timedelta(days=i) for i in range(6)]

    students = (
        db.query(Student)
        .filter(Student.group_id == group.id, Student.is_deleted == False)
        .order_by(Student.full_name)
        .all()
    )
    student_ids = [s.id for s in students]

    # Batch: get all lessons for the week
    lessons = db.query(Lesson).filter(
        Lesson.group_id == group.id,
        Lesson.lesson_date >= monday,
        Lesson.lesson_date <= saturday,
    ).all()
    lesson_by_date: Dict[date, Lesson] = {l.lesson_date: l for l in lessons}
    lesson_ids = [l.id for l in lessons]

    # Batch: attendance for all lessons this week
    att_map: Dict[tuple, Attendance] = {}
    if lesson_ids and student_ids:
        atts = db.query(Attendance).filter(
            Attendance.lesson_id.in_(lesson_ids),
            Attendance.student_id.in_(student_ids),
        ).all()
        for a in atts:
            att_map[(a.student_id, a.lesson_id)] = a

    nb_limit = int(get_system_setting(db, "NB_LIMIT_HIGH", "35"))

    students_data = []
    for s in students:
        days_data: Dict[str, Dict] = {}
        for d in days:
            lesson = lesson_by_date.get(d)
            att = att_map.get((s.id, lesson.id)) if lesson else None
            days_data[str(d)] = {
                "nb_hours": att.nb_hours if att else 0,
                "status": att.status if att else "present",
                "comment": att.comment if att else "",
                "is_reasoned": att.is_reasoned if att else False,
            }
        students_data.append(
            {
                "id": s.id,
                "full_name": s.full_name,
                "student_code": s.student_code,
                "total_absent_hours": s.total_absent_hours or 0,
                "is_high_risk": (s.total_absent_hours or 0) >= nb_limit,
                "days": days_data,
            }
        )

    return {
        "group_id": group.id,
        "group_number": group.number,
        "is_closed": bool(group.is_closed),
        "week_start": str(monday),
        "week_end": str(saturday),
        "days": [str(d) for d in days],
        "nb_limit": nb_limit,
        "students": students_data,
    }


# ─── JOURNAL: MARK DAY ────────────────────────────────────────────────────────

@router.post("/api/journal/mark-day")
async def mark_day(
    payload: Dict[str, Any] = Body(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Давомоти рӯзона.
    • mark_date метавонад ҳар рӯзи ҳафтаи ҷорӣ бошад (Душанбе..Шанбе)
    • Танҳо ҳафтаи ҷорӣ — НЕ ҳафтаи гузашта
    • NB-и қаблӣ DELETE НАМЕШАВАД — танҳо upsert
    • Гурӯҳи маҳкамшуда → 403
    """
    try:
        group = _get_open_group(db, current_user.id)
    except HTTPException:
        raise

    mark_date_str = payload.get("date")
    records = payload.get("records", [])

    if not mark_date_str:
        raise HTTPException(400, "Санаи дарс нишон дода нашудааст")

    try:
        mark_date = date.fromisoformat(mark_date_str)
    except ValueError:
        raise HTTPException(400, "Формати санаи нодуруст (YYYY-MM-DD)")

    monday, saturday = _week_bounds()
    if not (monday <= mark_date <= saturday):
        raise HTTPException(
            400,
            f"Танҳо ҳафтаи ҷорӣ ({monday} — {saturday}) таҳрир карда метавонед",
        )

    lesson = _get_or_create_lesson(db, group.id, mark_date)

    valid_sids = {
        s.id
        for s in db.query(Student).filter(
            Student.group_id == group.id, Student.is_deleted == False
        ).all()
    }

    updated: List[int] = []
    for rec in records:
        sid = rec.get("student_id")
        nb_hours = int(rec.get("nb_hours", 0))
        comment = str(rec.get("comment", ""))
        if sid not in valid_sids:
            continue
        if nb_hours < 0 or nb_hours > 8:
            continue
        _upsert_attendance(db, lesson.id, sid, nb_hours, comment, current_user.id)
        updated.append(sid)

    # Recalc NB totals — НЕ delete, танҳо ҷамъи нав
    for sid in updated:
        s = db.query(Student).filter(Student.id == sid).first()
        if s:
            s.total_absent_hours = _recalc_nb(db, sid)

    db.commit()
    _log(
        db,
        current_user.id,
        "DAY_MARKED",
        "attendance",
        group.id,
        f"{mark_date_str}: {len(updated)} students",
    )
    db.commit()
    return {"ok": True, "date": mark_date_str, "updated_count": len(updated)}


# ─── JOURNAL: MARK SINGLE STUDENT ─────────────────────────────────────────────

@router.post("/api/journal/mark")
async def mark_student(
    payload: Dict[str, Any] = Body(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Як донишҷӯи алоҳида — ҳамон ҳафтаи ҷорӣ. NB-и қаблӣ DELETE НАМЕШАВАД."""
    try:
        group = _get_open_group(db, current_user.id)
    except HTTPException:
        raise

    sid = payload.get("student_id")
    date_str = payload.get("date")
    nb_hours = int(payload.get("nb_hours", 0))
    comment = str(payload.get("comment", ""))

    if not sid or not date_str:
        raise HTTPException(400, "student_id ва date лозим аст")

    try:
        mark_date = date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(400, "Формати санаи нодуруст")

    monday, saturday = _week_bounds()
    if not (monday <= mark_date <= saturday):
        raise HTTPException(
            400,
            f"Танҳо ҳафтаи ҷорӣ ({monday} — {saturday}) таҳрир карда метавонед",
        )
    if nb_hours < 0 or nb_hours > 8:
        raise HTTPException(400, "nb_hours бояд байни 0 ва 8 бошад")

    student = db.query(Student).filter(
        Student.id == sid,
        Student.group_id == group.id,
        Student.is_deleted == False,
    ).first()
    if not student:
        raise HTTPException(404, "Донишҷӯ ёфт нашуд")

    lesson = _get_or_create_lesson(db, group.id, mark_date)
    _upsert_attendance(db, lesson.id, sid, nb_hours, comment, current_user.id)
    student.total_absent_hours = _recalc_nb(db, sid)
    db.commit()

    return {
        "ok": True,
        "student_id": sid,
        "date": date_str,
        "nb_hours": nb_hours,
        "total_absent_hours": student.total_absent_hours,
    }


# ─── JOURNAL: SAVE FULL WEEK ──────────────────────────────────────────────────

@router.post("/api/journal/save-week")
async def save_week(
    payload: Dict[str, Any] = Body(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Режими Ҳафтавӣ.
    • Танҳо Шанбе (weekday == 5) иҷозат дода мешавад
    • NB-и қаблӣ DELETE НАМЕШАВАД
    • Гурӯҳи маҳкамшуда → 403
    """
    try:
        group = _get_open_group(db, current_user.id)
    except HTTPException:
        raise

    today = date.today()
    if today.weekday() != 5:  # 5 = Saturday
        day_names = [
            "Душанбе",
            "Сешанбе",
            "Чоршанбе",
            "Панҷшанбе",
            "Ҷумъа",
            "Шанбе",
            "Якшанбе",
        ]
        raise HTTPException(
            400,
            f"Режими ҳафтавӣ танҳо рӯзи Шанбе дастрас аст. Имрӯз: {day_names[today.weekday()]}",
        )

    week_start_str = payload.get("week_start")
    days_data = payload.get("days", {})

    try:
        monday = date.fromisoformat(week_start_str)
        monday = monday - timedelta(days=monday.weekday())
    except (ValueError, TypeError):
        raise HTTPException(400, "week_start формати нодуруст")

    current_monday, _ = _week_bounds()
    if monday != current_monday:
        raise HTTPException(400, "Танҳо ҳафтаи ҷориро таҳрир карда метавонед")

    saturday = monday + timedelta(days=5)
    valid_sids = {
        s.id
        for s in db.query(Student).filter(
            Student.group_id == group.id, Student.is_deleted == False
        ).all()
    }

    updated_sids: set = set()
    total_updated = 0

    for date_str, records in days_data.items():
        try:
            d = date.fromisoformat(date_str)
        except ValueError:
            continue
        if not (monday <= d <= saturday):
            continue

        lesson = _get_or_create_lesson(db, group.id, d)
        for rec in records:
            sid = rec.get("student_id")
            nb_hours = int(rec.get("nb_hours", 0))
            comment = str(rec.get("comment", ""))
            if sid not in valid_sids:
                continue
            if nb_hours < 0 or nb_hours > 8:
                continue
            _upsert_attendance(db, lesson.id, sid, nb_hours, comment, current_user.id)
            updated_sids.add(sid)
            total_updated += 1

    for sid in updated_sids:
        s = db.query(Student).filter(Student.id == sid).first()
        if s:
            s.total_absent_hours = _recalc_nb(db, sid)

    db.commit()
    _log(
        db,
        current_user.id,
        "WEEK_SAVED",
        "attendance",
        group.id,
        f"Week {monday}: {total_updated} records",
    )
    db.commit()
    return {"ok": True, "week_start": str(monday), "total_updated": total_updated}


# ─── JOURNAL: STUDENT HISTORY ─────────────────────────────────────────────────

@router.get("/api/journal/student/{sid}")
async def student_attendance_history(
    sid: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Таърихи пурраи давомоти донишҷӯ."""
    try:
        group = _get_group(db, current_user.id)
    except HTTPException:
        raise HTTPException(403)

    student = db.query(Student).filter(
        Student.id == sid,
        Student.group_id == group.id,
        Student.is_deleted == False,
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
        "student_code": student.student_code,
        "total_absent_hours": student.total_absent_hours or 0,
        "records": [
            {
                "date": str(l.lesson_date),
                "status": a.status,
                "nb_hours": a.nb_hours,
                "comment": a.comment or "",
                "is_reasoned": a.is_reasoned,
                "reason_text": a.reason_text or "",
            }
            for a, l in records
            if (a.nb_hours or 0) > 0
        ],
    }


# ─── NB STATS ─────────────────────────────────────────────────────────────────

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
    students = (
        db.query(Student)
        .filter(Student.group_id == group.id, Student.is_deleted == False)
        .order_by(Student.total_absent_hours.desc())
        .all()
    )

    ranges = {"0": 0, "1-10": 0, "11-20": 0, "21-34": 0, "35+": 0}
    for s in students:
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
            {
                "id": s.id,
                "full_name": s.full_name,
                "student_code": s.student_code,
                "total_absent_hours": s.total_absent_hours or 0,
                "parent_phone": s.parent_phone,
                "birth_place": s.birth_place,
            }
            for s in students
            if (s.total_absent_hours or 0) >= nb_limit
        ],
        "all_students": [
            {"id": s.id, "full_name": s.full_name, "total_absent_hours": s.total_absent_hours or 0}
            for s in students
        ],
    }


# ─── PROFILE ──────────────────────────────────────────────────────────────────

@router.get("/api/profile")
async def get_profile(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    groups = (
        db.query(Group)
        .filter(Group.curator_id == current_user.id, Group.is_deleted == False)
        .all()
    )
    faculty = (
        db.query(Faculty).filter(Faculty.id == current_user.faculty_id).first()
        if current_user.faculty_id
        else None
    )

    return {
        "id": current_user.id,
        "full_name": current_user.full_name,
        "username": current_user.username,
        "email": current_user.email,
        "phone": current_user.phone,
        "department": current_user.department,
        "birth_year": current_user.birth_year,
        "faculty": faculty.name if faculty else None,
        "faculty_code": faculty.code if faculty else None,
        "faculty_id": current_user.faculty_id,
        "role": current_user.role.value if current_user.role else None,
        "token_version": current_user.token_version,
        "force_password_change": current_user.force_password_change,
        "is_deleted": current_user.is_deleted,
        "groups": [
            {
                "id": g.id,
                "number": g.number,
                "shift": g.shift,
                "is_closed": bool(g.is_closed),
                "is_active": g.is_active,
            }
            for g in groups
        ],
        "created_at": current_user.created_at.isoformat() if current_user.created_at else None,
        "updated_at": current_user.updated_at.isoformat() if current_user.updated_at else None,
    }


@router.put("/api/profile")
async def update_profile(
    payload: Dict[str, Any] = Body(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """JSON body (na Form) baroi hamohangī bо frontend fetch()."""
    if "full_name" in payload and payload["full_name"]:
        current_user.full_name = str(payload["full_name"]).strip()
    if "email" in payload:
        current_user.email = payload["email"] or None
    if "phone" in payload:
        current_user.phone = payload["phone"] or None
    if "department" in payload:
        current_user.department = payload["department"] or None
    if "birth_year" in payload and payload["birth_year"]:
        current_user.birth_year = int(payload["birth_year"])
    db.commit()
    _log(db, current_user.id, "PROFILE_UPDATED", "users", current_user.id, current_user.full_name)
    db.commit()
    return {"id": current_user.id, "full_name": current_user.full_name}


# ─── SUPERVISORS ──────────────────────────────────────────────────────────────

@router.get("/api/supervisors")
async def get_supervisors(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Профили роҳбарияти факулта ва ректорат."""
    if not current_user.faculty_id:
        return []
    result = []
    dean = db.query(User).filter(
        User.role == UserRole.DEAN,
        User.faculty_id == current_user.faculty_id,
        User.is_deleted == False,
    ).first()
    if dean:
        result.append(
            {
                "role": "Декан",
                "id": dean.id,
                "full_name": dean.full_name,
                "email": dean.email,
                "phone": dean.phone,
                "department": dean.department,
            }
        )
    for vd in db.query(User).filter(
        User.role == UserRole.VICE_DEAN,
        User.faculty_id == current_user.faculty_id,
        User.is_deleted == False,
    ).all():
        result.append(
            {
                "role": "Замдекан",
                "id": vd.id,
                "full_name": vd.full_name,
                "email": vd.email,
                "phone": vd.phone,
                "department": vd.department,
            }
        )
    rector = db.query(User).filter(
        User.role == UserRole.RECTOR, User.is_deleted == False
    ).first()
    if rector:
        result.append(
            {
                "role": "Ректор",
                "id": rector.id,
                "full_name": rector.full_name,
                "email": rector.email,
                "phone": rector.phone,
                "department": rector.department,
            }
        )
    return result


# ─── CHANGE PASSWORD ──────────────────────────────────────────────────────────

@router.get("/change-password", response_class=HTMLResponse)
async def change_password_page(
    request: Request, current_user: User = Depends(get_current_user)
):
    return templates.TemplateResponse(
        "change_password.html", {"request": request, "user": current_user}
    )


@router.post("/change-password", response_class=HTMLResponse)
async def change_password_post(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    form = await request.form()
    new_password = str(form.get("new_password", ""))
    valid, msg = validate_password_policy(new_password)
    if not valid:
        return templates.TemplateResponse(
            "change_password.html",
            {"request": request, "user": current_user, "error": msg},
        )
    current_user.password_hash = get_password_hash(new_password)
    current_user.force_password_change = False
    current_user.token_version += 1
    db.commit()
    token = create_access_token(
        {
            "sub": str(current_user.id),
            "ver": current_user.token_version,
            "role": current_user.role.value,
        }
    )
    response = RedirectResponse("/curator/dashboard", status_code=303)
    response.set_cookie(
        key="access_token",
        value=f"Bearer {token}",
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
        max_age=ACCESS_TOKEN_EXPIRE_HOURS * 3600,
    )
    return response