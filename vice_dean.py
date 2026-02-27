"""
vice_dean.py — SENIOR REFACTOR v4.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Ислоҳоти асосӣ:
  1. Интихоби куратор ба гурӯҳ (assign curator → group)
  2. Маҳкам кардани гурӯҳ (close_group) — баъди маҳкамшавӣ ҳеҷ амале нест
  3. Дидани профили тамоми кормандон ва донишҷӯён
  4. Мониторинг: донишҷӯёни хавфнок + гурӯҳҳои бе-давомот
  5. Ҳамаи profiles бо маълумоти пурра аз модел
"""
from __future__ import annotations

import csv
import io
from datetime import date, timedelta, datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jose import jwt
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from config import ALGORITHM, SECRET_KEY, templates
from dependencies import get_current_user, get_db
from models import (
    Attendance,
    AuditLog,
    AcademicYear,
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
    target_id: Optional[int] = None,
    desc: str = "",
) -> None:
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


def get_vd(current_user: User = Depends(get_current_user)) -> User:
    if not current_user or current_user.role != UserRole.VICE_DEAN:
        raise HTTPException(403, "Дастрасӣ нест")
    if not current_user.faculty_id:
        raise HTTPException(403, "Факултет таъин нашудааст")
    return current_user


def _week_bounds(target: Optional[date] = None):
    if target is None:
        target = date.today()
    monday = target - timedelta(days=target.weekday())
    saturday = monday + timedelta(days=5)
    return monday, saturday


def _att_pct_for_groups(db: Session, group_ids: List[int], target: date):
    """Returns (pct, present, recorded) for given groups on given date."""
    if not group_ids:
        return 0.0, 0, 0
    lesson_ids = [
        lid
        for (lid,) in db.query(Lesson.id)
        .filter(Lesson.group_id.in_(group_ids), Lesson.lesson_date == target)
        .all()
    ]
    if not lesson_ids:
        return 0.0, 0, 0
    counts = dict(
        db.query(Attendance.status, func.count(Attendance.id))
        .filter(Attendance.lesson_id.in_(lesson_ids))
        .group_by(Attendance.status)
        .all()
    )
    present = int(counts.get("present", 0) or 0)
    absent = int(counts.get("absent", 0) or 0)
    recorded = present + absent
    pct = round(present / recorded * 100.0, 1) if recorded else 0.0
    return pct, present, recorded


def _faculty_group_ids(db: Session, faculty_id: int) -> List[int]:
    return [
        gid
        for (gid,) in db.query(Group.id)
        .filter(Group.faculty_id == faculty_id, Group.is_deleted == False)
        .all()
    ]


# ─── DASHBOARD ────────────────────────────────────────────────────────────────

@router.get("/dashboard", response_class=HTMLResponse)
async def vice_dean_dashboard(
    request: Request,
    current_user: User = Depends(get_vd),
    db: Session = Depends(get_db),
):
    faculty = db.query(Faculty).filter(Faculty.id == current_user.faculty_id).first()
    return templates.TemplateResponse(
        "zamdean.html",
        {
            "request": request,
            "user": current_user,
            "faculty": faculty,
            "current_year": date.today().year,
        },
    )


# ─── API: OVERVIEW STATS ─────────────────────────────────────────────────────

@router.get("/api/stats")
@router.get("/api/stats/overview")
async def overview_stats(
    current_user: User = Depends(get_vd),
    db: Session = Depends(get_db),
):
    fid = current_user.faculty_id
    nb_limit = int(get_system_setting(db, "NB_LIMIT_HIGH", "35"))
    today = date.today()

    total_students = (
        db.query(Student)
        .join(Group)
        .filter(Group.faculty_id == fid, Student.is_deleted == False)
        .count()
    )
    total_groups = db.query(Group).filter(
        Group.faculty_id == fid, Group.is_deleted == False
    ).count()
    active_groups = db.query(Group).filter(
        Group.faculty_id == fid,
        Group.is_deleted == False,
        Group.is_active != False,
    ).count()
    total_curators = db.query(User).filter(
        User.faculty_id == fid,
        User.role == UserRole.CURATOR,
        User.is_deleted == False,
    ).count()
    high_abs = (
        db.query(Student)
        .join(Group)
        .filter(
            Group.faculty_id == fid,
            Student.total_absent_hours >= nb_limit,
            Student.is_deleted == False,
        )
        .count()
    )

    group_ids = _faculty_group_ids(db, fid)
    att_pct, _, _ = _att_pct_for_groups(db, group_ids, today)

    # Groups without today's attendance (only active, non-closed)
    active_open_ids = [
        gid
        for (gid,) in db.query(Group.id).filter(
            Group.faculty_id == fid,
            Group.is_deleted == False,
            Group.is_active != False,
        ).all()
    ]
    filled_today = {
        gid
        for (gid,) in db.query(Lesson.group_id).filter(
            Lesson.group_id.in_(active_open_ids), Lesson.lesson_date == today
        ).all()
    }
    groups_no_att = len(active_open_ids) - len(filled_today)

    courses = db.query(Course).order_by(Course.year).all()
    course_stats = []
    for c in courses:
        gc = db.query(Group).filter(
            Group.faculty_id == fid, Group.course_id == c.id, Group.is_deleted == False
        ).count()
        sc = (
            db.query(Student)
            .join(Group)
            .filter(
                Group.faculty_id == fid,
                Group.course_id == c.id,
                Student.is_deleted == False,
            )
            .count()
        )
        if gc > 0:
            course_stats.append({"year": c.year, "groups": gc, "students": sc})

    # Top 5 at-risk students
    high_risk_students = (
        db.query(Student)
        .join(Group)
        .filter(
            Group.faculty_id == fid,
            Student.total_absent_hours >= nb_limit,
            Student.is_deleted == False,
        )
        .options(joinedload(Student.group))
        .order_by(Student.total_absent_hours.desc())
        .limit(5)
        .all()
    )

    return {
        "total_students": total_students,
        "total_groups": total_groups,
        "active_groups": active_groups,
        "total_curators": total_curators,
        "high_absence_students": high_abs,
        "high_risk_count": high_abs,
        "nb_limit": nb_limit,
        "attendance_rate": att_pct,
        "groups_no_attendance_today": groups_no_att,
        "course_stats": course_stats,
        "high_risk": [
            {
                "id": s.id,
                "full_name": s.full_name,
                "total_absent_hours": int(s.total_absent_hours or 0),
                "group_number": s.group.number if s.group else None,
                "group_id": s.group_id,
            }
            for s in high_risk_students
        ],
    }


# ─── API: GROUPS ──────────────────────────────────────────────────────────────

@router.get("/api/groups")
async def list_groups(
    current_user: User = Depends(get_vd),
    db: Session = Depends(get_db),
):
    today = date.today()
    fid = current_user.faculty_id
    groups = (
        db.query(Group)
        .filter(Group.faculty_id == fid, Group.is_deleted == False)
        .options(joinedload(Group.course), joinedload(Group.curator))
        .order_by(Group.number)
        .all()
    )

    group_ids = [g.id for g in groups]

    # Batch student counts
    stu_counts = (
        {
            gid: cnt
            for gid, cnt in db.query(Student.group_id, func.count(Student.id))
            .filter(Student.group_id.in_(group_ids), Student.is_deleted == False)
            .group_by(Student.group_id)
            .all()
        }
        if group_ids
        else {}
    )

    # Today's attendance status per group
    lessons_today = (
        {
            gid
            for (gid,) in db.query(Lesson.group_id).filter(
                Lesson.group_id.in_(group_ids), Lesson.lesson_date == today
            ).all()
        }
        if group_ids
        else set()
    )

    return [
        {
            "id": g.id,
            "number": g.number,
            "shift": g.shift,
            "course_id": g.course_id,
            "course_year": g.course.year if g.course else None,
            "academic_year_id": g.academic_year_id,
            "faculty_id": g.faculty_id,
            "curator_id": g.curator_id,
            "curator_name": g.curator.full_name if g.curator else None,
            "curator_username": g.curator.username if g.curator else None,
            "curator_phone": g.curator.phone if g.curator else None,
            "curator_department": g.curator.department if g.curator else None,
            "is_active": g.is_active,
            "is_closed": bool(getattr(g, "is_closed", False)),
            "is_deleted": g.is_deleted,
            "total_students": stu_counts.get(g.id, 0),
            "student_count": stu_counts.get(g.id, 0),
            "attendance_today": g.id in lessons_today,
            "created_at": g.created_at.isoformat() if g.created_at else None,
            "updated_at": g.updated_at.isoformat() if g.updated_at else None,
        }
        for g in groups
    ]


@router.post("/api/groups")
async def create_group(
    payload: Dict[str, Any] = Body(...),
    current_user: User = Depends(get_vd),
    db: Session = Depends(get_db),
):
    """
    JSON body: {number, shift, course_id, curator_id?}
    Uses raw SQL INSERT because Group ORM model does not expose the `name` column
    which has a NOT NULL constraint in the database.
    """
    from sqlalchemy import text

    number    = (payload.get("number") or "").strip()
    shift     = int(payload.get("shift", 1))
    course_id = payload.get("course_id")
    curator_id = payload.get("curator_id") or None

    if not number:
        raise HTTPException(400, "Рақами гурӯҳ нишон дода нашудааст")
    if not course_id:
        raise HTTPException(400, "Курс нишон дода нашудааст")
    if shift not in (1, 2):
        raise HTTPException(400, "Навбат бояд 1 ё 2 бошад")

    # ── academic year ────────────────────────────────────────────────────────
    ay_row = db.execute(text(
        "SELECT id FROM academic_years WHERE is_current = true LIMIT 1"
    )).fetchone()
    if not ay_row:
        ay_row = db.execute(text(
            "SELECT id FROM academic_years ORDER BY id DESC LIMIT 1"
        )).fetchone()
    if not ay_row:
        raise HTTPException(400, "Соли таҳсили ҷорӣ муайян нашудааст")
    academic_year_id = ay_row.id

    # ── validate course ──────────────────────────────────────────────────────
    course_row = db.execute(
        text("SELECT id FROM courses WHERE id = :cid"),
        {"cid": int(course_id)},
    ).fetchone()
    if not course_row:
        raise HTTPException(400, "Курс ёфт нашуд")

    # ── validate curator ─────────────────────────────────────────────────────
    if curator_id:
        cur_row = db.execute(text("""
            SELECT id FROM users
            WHERE id = :uid AND faculty_id = :fid
              AND role = 'curator' AND is_deleted = false
        """), {"uid": int(curator_id), "fid": current_user.faculty_id}).fetchone()
        if not cur_row:
            raise HTTPException(400, "Куратор ёфт нашуд ё ба ин факултет мансуб нест")

    # ── duplicate check ───────────────────────────────────────────────────────
    dup = db.execute(text("""
        SELECT id FROM groups
        WHERE number = :num AND faculty_id = :fid AND is_deleted = false
    """), {"num": number, "fid": current_user.faculty_id}).fetchone()
    if dup:
        raise HTTPException(400, f"Гурӯҳ «{number}» аллакай мавҷуд аст")

    # ── INSERT with raw SQL (name column is NOT NULL in DB) ───────────────────
    result = db.execute(text("""
        INSERT INTO groups (
            name, number, shift, course_id,
            academic_year_id, faculty_id, curator_id,
            is_active
        ) VALUES (
            :name, :number, :shift, :course_id,
            :academic_year_id, :faculty_id, :curator_id,
            true
        )
        RETURNING id, number
    """), {
        "name":             number,
        "number":           number,
        "shift":            shift,
        "course_id":        int(course_id),
        "academic_year_id": academic_year_id,
        "faculty_id":       current_user.faculty_id,
        "curator_id":       int(curator_id) if curator_id else None,
    })

    row = result.fetchone()
    db.commit()
    _log(db, current_user.id, "GROUP_CREATED", "groups", row.id, number)
    db.commit()
    return {"id": row.id, "number": row.number, "ok": True}


@router.put("/api/groups/{gid}")
async def update_group(
    gid: int,
    payload: Dict[str, Any] = Body(...),
    current_user: User = Depends(get_vd),
    db: Session = Depends(get_db),
):
    g = db.query(Group).filter(
        Group.id == gid,
        Group.faculty_id == current_user.faculty_id,
        Group.is_deleted == False,
    ).first()
    if not g:
        raise HTTPException(404, "Гурӯҳ ёфт нашуд")
    if getattr(g, "is_closed", False):
        raise HTTPException(400, "Гурӯҳи маҳкам шуда таҳрир карда намешавад")

    if "number" in payload and payload["number"]:
        g.number = str(payload["number"]).strip()
    if "shift" in payload and payload["shift"] in (1, 2):
        g.shift = int(payload["shift"])
    if "course_id" in payload and payload["course_id"]:
        g.course_id = int(payload["course_id"])
    if "curator_id" in payload:
        cid = payload["curator_id"]
        if cid:
            c = db.query(User).filter(
                User.id == int(cid),
                User.role == UserRole.CURATOR,
                User.faculty_id == current_user.faculty_id,
                User.is_deleted == False,
            ).first()
            if not c:
                raise HTTPException(400, "Куратор ёфт нашуд")
            g.curator_id = int(cid)
            g.is_active = True
        else:
            g.curator_id = None
    if "is_active" in payload:
        g.is_active = bool(payload["is_active"])

    _log(db, current_user.id, "GROUP_UPDATED", "groups", gid, g.number)
    db.commit()
    return {"ok": True}


@router.post("/api/groups/{gid}/close")
async def close_group(
    gid: int,
    current_user: User = Depends(get_vd),
    db: Session = Depends(get_db),
):
    """
    Маҳкам кардани гурӯҳ (барои курси 4 ва ғайра).
    Баъди маҳкамшавӣ:
      • is_closed = True
      • is_active = False
      • Куратор наметавонад давомот сабт кунад
      • Ҳеҷ таҳрире иҷозат дода намешавад
      • Статистикаи пешин дасту нахӯрда мемонад
    """
    g = db.query(Group).filter(
        Group.id == gid,
        Group.faculty_id == current_user.faculty_id,
        Group.is_deleted == False,
    ).first()
    if not g:
        raise HTTPException(404, "Гурӯҳ ёфт нашуд")
    if getattr(g, "is_closed", False):
        raise HTTPException(400, "Гурӯҳ аллакай маҳкам шудааст")

    try:
        g.is_closed = True
    except Exception:
        pass
    g.is_active = False
    _log(
        db,
        current_user.id,
        "GROUP_CLOSED",
        "groups",
        gid,
        f"Гурӯҳ {g.number} маҳкам шуд",
    )
    db.commit()
    return {"ok": True, "group_id": gid, "number": g.number, "is_closed": True}


@router.post("/api/groups/{gid}/reopen")
async def reopen_group(
    gid: int,
    current_user: User = Depends(get_vd),
    db: Session = Depends(get_db),
):
    """Кушодани гурӯҳи маҳкамшуда (агар зарур бошад)."""
    g = db.query(Group).filter(
        Group.id == gid,
        Group.faculty_id == current_user.faculty_id,
        Group.is_deleted == False,
    ).first()
    if not g:
        raise HTTPException(404, "Гурӯҳ ёфт нашуд")
    if not getattr(g, "is_closed", False):
        raise HTTPException(400, "Гурӯҳ маҳкам нашудааст")

    try:
        g.is_closed = False
    except Exception:
        pass
    g.is_active = True
    _log(db, current_user.id, "GROUP_REOPENED", "groups", gid, g.number)
    db.commit()
    return {"ok": True, "group_id": gid, "is_closed": False}


@router.post("/api/groups/{gid}/assign-curator")
async def assign_curator(
    gid: int,
    payload: Dict[str, Any] = Body(...),
    current_user: User = Depends(get_vd),
    db: Session = Depends(get_db),
):
    """
    Таъин / бардошти куратор ба гурӯҳ.
    JSON: {curator_id: int | null}
    """
    g = db.query(Group).filter(
        Group.id == gid,
        Group.faculty_id == current_user.faculty_id,
        Group.is_deleted == False,
    ).first()
    if not g:
        raise HTTPException(404, "Гурӯҳ ёфт нашуд")
    if getattr(g, "is_closed", False):
        raise HTTPException(400, "Ба гурӯҳи маҳкамшуда куратор таъин карда намешавад")

    cid = payload.get("curator_id")
    if cid:
        c = db.query(User).filter(
            User.id == int(cid),
            User.role == UserRole.CURATOR,
            User.faculty_id == current_user.faculty_id,
            User.is_deleted == False,
        ).first()
        if not c:
            raise HTTPException(400, "Куратор ёфт нашуд ё ба ин факултет мансуб нест")
        g.curator_id = int(cid)
        g.is_active = True
        _log(
            db,
            current_user.id,
            "CURATOR_ASSIGNED",
            "groups",
            gid,
            f"{c.full_name} → {g.number}",
        )
    else:
        g.curator_id = None
        _log(db, current_user.id, "CURATOR_REMOVED", "groups", gid, g.number)

    db.commit()
    return {"ok": True, "group_id": gid, "curator_id": cid}


@router.delete("/api/groups/{gid}")
async def delete_group(
    gid: int,
    current_user: User = Depends(get_vd),
    db: Session = Depends(get_db),
):
    g = db.query(Group).filter(
        Group.id == gid,
        Group.faculty_id == current_user.faculty_id,
        Group.is_deleted == False,
    ).first()
    if not g:
        raise HTTPException(404, "Гурӯҳ ёфт нашуд")
    g.is_deleted = True
    _log(db, current_user.id, "GROUP_DELETED", "groups", gid, g.number)
    db.commit()
    return {"ok": True}


# ─── API: STUDENTS ────────────────────────────────────────────────────────────

@router.get("/api/students")
async def list_students(
    group_id: Optional[int] = Query(None),
    search: Optional[str] = Query(None),
    course_year: Optional[int] = Query(None),
    course: Optional[int] = Query(None),
    high_nb: Optional[bool] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    current_user: User = Depends(get_vd),
    db: Session = Depends(get_db),
):
    fid = current_user.faculty_id
    nb_limit = int(get_system_setting(db, "NB_LIMIT_HIGH", "35"))
    q = (
        db.query(Student)
        .join(Group)
        .filter(Group.faculty_id == fid, Student.is_deleted == False)
        .options(joinedload(Student.group).joinedload(Group.course))
    )
    if group_id:
        q = q.filter(Student.group_id == group_id)
    if search and len(search) >= 2:
        q = q.filter(Student.full_name.ilike(f"%{search}%"))
    cy = course_year or course
    if cy:
        q = q.filter(
            Group.course_id.in_(db.query(Course.id).filter(Course.year == cy))
        )
    if high_nb:
        q = q.filter(Student.total_absent_hours >= nb_limit)

    total = q.count()
    students = (
        q.order_by(Student.full_name)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "nb_limit": nb_limit,
        "students": [
            {
                "id": s.id,
                "full_name": s.full_name,
                "student_code": s.student_code,
                "group_number": s.group.number if s.group else None,
                "group_id": s.group_id,
                "course_year": s.group.course.year if s.group and s.group.course else None,
                "total_absences": int(s.total_absent_hours or 0),
                "total_absent_hours": int(s.total_absent_hours or 0),
                "birth_place": s.birth_place or "",
                "region": s.region or "",
                "parent_phone": s.parent_phone or "",
                "is_high_risk": (s.total_absent_hours or 0) >= nb_limit,
            }
            for s in students
        ],
    }


@router.get("/api/students/{sid}")
async def get_student(
    sid: int,
    current_user: User = Depends(get_vd),
    db: Session = Depends(get_db),
):
    """Профили пурраи донишҷӯ бо таърихи ҳамаи NB-ҳо ва санаи дақиқ."""
    fid = current_user.faculty_id
    s = (
        db.query(Student)
        .join(Group)
        .filter(
            Student.id == sid, Group.faculty_id == fid, Student.is_deleted == False
        )
        .options(joinedload(Student.group).joinedload(Group.course))
        .first()
    )
    if not s:
        raise HTTPException(404, "Донишҷӯ ёфт нашуд")

    nb_limit = int(get_system_setting(db, "NB_LIMIT_HIGH", "35"))

    # Ҳамаи NB-ҳо бо санаи дақиқ (sorted by date desc)
    records = (
        db.query(Attendance, Lesson)
        .join(Lesson, Attendance.lesson_id == Lesson.id)
        .filter(Attendance.student_id == sid, Attendance.status == "absent")
        .order_by(Lesson.lesson_date.desc())
        .all()
    )

    return {
        # ── Identity ──────────────────────────────────────────────────────
        "id": s.id,
        "full_name": s.full_name,
        "student_code": s.student_code,
        # ── Group / Faculty ────────────────────────────────────────────────
        "group_number": s.group.number if s.group else None,
        "group_id": s.group_id,
        "group_shift": s.group.shift if s.group else None,
        "course_year": s.group.course.year if s.group and s.group.course else None,
        "faculty_id": s.faculty_id,
        # ── Personal ──────────────────────────────────────────────────────
        "birth_year": s.birth_year,
        "birth_place": s.birth_place or "",
        "region": s.region or "",
        "parent_phone": s.parent_phone or "",
        # ── Education ─────────────────────────────────────────────────────
        "study_start": str(s.study_start) if s.study_start else None,
        "expected_graduation": str(s.expected_graduation) if s.expected_graduation else None,
        # ── Attendance ────────────────────────────────────────────────────
        "total_absent_hours": int(s.total_absent_hours or 0),
        "is_high_risk": (s.total_absent_hours or 0) >= nb_limit,
        "nb_limit": nb_limit,
        # ── NB History (ҳамаи NB-ҳо бо санаи дақиқ) ─────────────────────
        "nb_history": [
            {
                "date": str(l.lesson_date),
                "nb_hours": a.nb_hours,
                "comment": a.comment or "",
                "is_reasoned": a.is_reasoned,
                "reason_text": a.reason_text or "",
            }
            for a, l in records
        ],
        # ── Timestamps ────────────────────────────────────────────────────
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
        "is_deleted": s.is_deleted,
    }


# ─── API: AT-RISK ─────────────────────────────────────────────────────────────

@router.get("/api/at-risk")
async def at_risk(
    group_id: Optional[int] = Query(None),
    current_user: User = Depends(get_vd),
    db: Session = Depends(get_db),
):
    fid = current_user.faculty_id
    nb_limit = int(get_system_setting(db, "NB_LIMIT_HIGH", "35"))
    q = (
        db.query(Student)
        .join(Group)
        .filter(
            Group.faculty_id == fid,
            Student.total_absent_hours >= nb_limit,
            Student.is_deleted == False,
        )
        .options(joinedload(Student.group))
    )
    if group_id:
        q = q.filter(Student.group_id == group_id)
    students = q.order_by(Student.total_absent_hours.desc()).all()
    return [
        {
            "id": s.id,
            "full_name": s.full_name,
            "student_code": s.student_code,
            "group_number": s.group.number if s.group else None,
            "group_id": s.group_id,
            "total_absent_hours": int(s.total_absent_hours or 0),
            "parent_phone": s.parent_phone,
        }
        for s in students
    ]


# ─── API: MONITORING — Groups without attendance today ────────────────────────

@router.get("/api/monitoring/no-attendance")
async def groups_without_attendance(
    target_date: Optional[str] = Query(None),
    current_user: User = Depends(get_vd),
    db: Session = Depends(get_db),
):
    """
    Гурӯҳҳое, ки журналро пур накардаанд.
    Танҳо гурӯҳҳои фаъол ва кушода (is_closed=False) ҳисоб мешаванд.
    """
    fid = current_user.faculty_id
    d = date.fromisoformat(target_date) if target_date else date.today()

    groups = (
        db.query(Group)
        .filter(
            Group.faculty_id == fid,
            Group.is_deleted == False,
            Group.is_active != False,
        )
        .options(joinedload(Group.curator), joinedload(Group.course))
        .order_by(Group.number)
        .all()
    )

    if not groups:
        return {"date": str(d), "total_groups": 0, "filled": 0, "missing": 0, "groups": []}

    group_ids = [g.id for g in groups]
    filled = {
        gid
        for (gid,) in db.query(Lesson.group_id).filter(
            Lesson.group_id.in_(group_ids), Lesson.lesson_date == d
        ).all()
    }

    # Student counts per group
    stu_counts = {
        gid: cnt
        for gid, cnt in db.query(Student.group_id, func.count(Student.id))
        .filter(Student.group_id.in_(group_ids), Student.is_deleted == False)
        .group_by(Student.group_id)
        .all()
    }

    result = [
        {
            "group_id": g.id,
            "group_number": g.number,
            "shift": g.shift,
            "course_year": g.course.year if g.course else None,
            "curator_name": g.curator.full_name if g.curator else None,
            "curator_phone": g.curator.phone if g.curator else None,
            "total_students": stu_counts.get(g.id, 0),
            "has_attendance": g.id in filled,
        }
        for g in groups
    ]

    return {
        "date": str(d),
        "total_groups": len(result),
        "filled": sum(1 for r in result if r["has_attendance"]),
        "missing": sum(1 for r in result if not r["has_attendance"]),
        "groups": result,
    }


# ─── API: CURATORS ────────────────────────────────────────────────────────────

@router.get("/api/curators")
async def list_curators(
    current_user: User = Depends(get_vd),
    db: Session = Depends(get_db),
):
    fid = current_user.faculty_id
    curators = (
        db.query(User)
        .filter(
            User.role == UserRole.CURATOR,
            User.faculty_id == fid,
            User.is_deleted == False,
        )
        .order_by(User.full_name)
        .all()
    )

    curator_ids = [c.id for c in curators]
    grp_map: Dict[int, Dict] = {}
    if curator_ids:
        for cid, gid, gnum in db.query(
            Group.curator_id, Group.id, Group.number
        ).filter(
            Group.curator_id.in_(curator_ids),
            Group.is_deleted == False,
            Group.is_active != False,
        ).all():
            if cid not in grp_map:
                grp_map[cid] = {"id": gid, "number": gnum}

    return [
        {
            "id": c.id,
            "full_name": c.full_name,
            "username": c.username,
            "role": c.role.value if c.role else None,
            "email": c.email,
            "phone": c.phone,
            "department": c.department,
            "birth_year": c.birth_year,
            "faculty_id": c.faculty_id,
            "group": grp_map.get(c.id),
            "group_id": grp_map[c.id]["id"] if c.id in grp_map else None,
            "group_number": grp_map[c.id]["number"] if c.id in grp_map else None,
            "force_password_change": c.force_password_change,
            "token_version": c.token_version,
            "is_deleted": c.is_deleted,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "updated_at": c.updated_at.isoformat() if c.updated_at else None,
        }
        for c in curators
    ]


@router.get("/api/curators/{uid}")
async def get_curator(
    uid: int,
    current_user: User = Depends(get_vd),
    db: Session = Depends(get_db),
):
    c = db.query(User).filter(
        User.id == uid,
        User.role == UserRole.CURATOR,
        User.faculty_id == current_user.faculty_id,
        User.is_deleted == False,
    ).first()
    if not c:
        raise HTTPException(404, "Куратор ёфт нашуд")

    all_groups = (
        db.query(Group)
        .filter(Group.curator_id == c.id, Group.is_deleted == False)
        .options(joinedload(Group.course))
        .all()
    )

    return {
        "id": c.id,
        "full_name": c.full_name,
        "username": c.username,
        "role": c.role.value if c.role else None,
        "email": c.email,
        "phone": c.phone,
        "department": c.department,
        "birth_year": c.birth_year,
        "faculty_id": c.faculty_id,
        "group_id": all_groups[0].id if all_groups else None,
        "group_number": all_groups[0].number if all_groups else None,
        "groups": [
            {
                "id": g.id,
                "number": g.number,
                "shift": g.shift,
                "course_year": g.course.year if g.course else None,
                "is_active": g.is_active,
                "is_closed": bool(getattr(g, "is_closed", False)),
            }
            for g in all_groups
        ],
        "force_password_change": c.force_password_change,
        "token_version": c.token_version,
        "is_deleted": c.is_deleted,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


@router.post("/api/curators")
async def create_curator(
    payload: Dict[str, Any] = Body(...),
    current_user: User = Depends(get_vd),
    db: Session = Depends(get_db),
):
    full_name = (payload.get("full_name") or "").strip()
    username = (payload.get("username") or "").strip()
    department = payload.get("department") or None
    email = payload.get("email") or None
    phone = payload.get("phone") or None
    birth_year = payload.get("birth_year") or None

    if not full_name:
        raise HTTPException(400, "Ному насаб лозим аст")
    if not username:
        raise HTTPException(400, "Номи корбар лозим аст")
    if db.query(User).filter(User.username == username).first():
        raise HTTPException(400, "Бо ин юзернейм корбар аллакай мавҷуд аст")

    u = User(
        full_name=full_name,
        username=username,
        password_hash=get_password_hash("020304"),
        role=UserRole.CURATOR,
        faculty_id=current_user.faculty_id,
        department=department,
        email=email,
        phone=phone,
        birth_year=int(birth_year) if birth_year else None,
        force_password_change=True,
        token_version=1,
    )
    db.add(u)
    db.flush()
    _log(db, current_user.id, "CURATOR_CREATED", "users", u.id, full_name)
    db.commit()
    return {"id": u.id, "full_name": u.full_name, "username": u.username, "ok": True}


@router.put("/api/curators/{uid}")
async def update_curator(
    uid: int,
    payload: Dict[str, Any] = Body(...),
    current_user: User = Depends(get_vd),
    db: Session = Depends(get_db),
):
    u = db.query(User).filter(
        User.id == uid,
        User.role == UserRole.CURATOR,
        User.faculty_id == current_user.faculty_id,
        User.is_deleted == False,
    ).first()
    if not u:
        raise HTTPException(404, "Куратор ёфт нашуд")

    if "full_name" in payload and payload["full_name"]:
        u.full_name = str(payload["full_name"]).strip()
    if "department" in payload:
        u.department = payload["department"] or None
    if "email" in payload:
        u.email = payload["email"] or None
    if "phone" in payload:
        u.phone = payload["phone"] or None
    if "birth_year" in payload and payload["birth_year"]:
        u.birth_year = int(payload["birth_year"])

    _log(db, current_user.id, "CURATOR_UPDATED", "users", uid, u.full_name)
    db.commit()
    return {"id": u.id, "full_name": u.full_name, "ok": True}


@router.post("/api/curators/{uid}/reset-password")
async def reset_curator_password(
    uid: int,
    current_user: User = Depends(get_vd),
    db: Session = Depends(get_db),
):
    u = db.query(User).filter(
        User.id == uid,
        User.role == UserRole.CURATOR,
        User.faculty_id == current_user.faculty_id,
        User.is_deleted == False,
    ).first()
    if not u:
        raise HTTPException(404, "Куратор ёфт нашуд")
    u.password_hash = get_password_hash("020304")
    u.force_password_change = True
    u.token_version += 1
    _log(db, current_user.id, "PASSWORD_RESET", "users", uid)
    db.commit()
    return {"ok": True, "new_password": "020304"}


@router.delete("/api/curators/{uid}")
async def delete_curator(
    uid: int,
    current_user: User = Depends(get_vd),
    db: Session = Depends(get_db),
):
    u = db.query(User).filter(
        User.id == uid,
        User.role == UserRole.CURATOR,
        User.faculty_id == current_user.faculty_id,
        User.is_deleted == False,
    ).first()
    if not u:
        raise HTTPException(404, "Куратор ёфт нашуд")
    u.is_deleted = True
    u.token_version += 1
    _log(db, current_user.id, "CURATOR_DELETED", "users", uid)
    db.commit()
    return {"ok": True}


# ─── API: SUPERVISORS (Dean, Rector profiles) ────────────────────────────────

@router.get("/api/supervisors")
async def get_supervisors(
    current_user: User = Depends(get_vd),
    db: Session = Depends(get_db),
):
    """Профили роҳбарият — Декан ва Ректор."""
    fid = current_user.faculty_id
    result = []
    dean = db.query(User).filter(
        User.role == UserRole.DEAN, User.faculty_id == fid, User.is_deleted == False
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


# ─── API: ALL STAFF PROFILES ─────────────────────────────────────────────────

@router.get("/api/staff")
async def list_staff(
    current_user: User = Depends(get_vd),
    db: Session = Depends(get_db),
):
    """Рӯйхати тамоми кормандони факулта (декан, замдекан, куратор)."""
    fid = current_user.faculty_id
    users = (
        db.query(User)
        .filter(
            User.faculty_id == fid,
            User.is_deleted == False,
            User.role.in_([UserRole.DEAN, UserRole.VICE_DEAN, UserRole.CURATOR]),
        )
        .order_by(User.role, User.full_name)
        .all()
    )
    return [
        {
            "id": u.id,
            "full_name": u.full_name,
            "username": u.username,
            "role": u.role.value,
            "email": u.email,
            "phone": u.phone,
            "department": u.department,
            "birth_year": u.birth_year,
            "faculty_id": u.faculty_id,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        }
        for u in users
    ]


# ─── API: COURSES ─────────────────────────────────────────────────────────────

@router.get("/api/courses")
async def list_courses(db: Session = Depends(get_db)):
    return [
        {"id": c.id, "year": c.year}
        for c in db.query(Course).order_by(Course.year).all()
    ]


# ─── API: PROFILE ─────────────────────────────────────────────────────────────

@router.get("/api/profile")
async def get_profile(
    current_user: User = Depends(get_vd),
    db: Session = Depends(get_db),
):
    faculty = db.query(Faculty).filter(Faculty.id == current_user.faculty_id).first()
    return {
        "id": current_user.id,
        "full_name": current_user.full_name,
        "username": current_user.username,
        "role": current_user.role.value,
        "email": current_user.email,
        "phone": current_user.phone,
        "department": current_user.department,
        "birth_year": current_user.birth_year,
        "faculty_id": current_user.faculty_id,
        "faculty": faculty.name if faculty else None,
        "faculty_name": faculty.name if faculty else None,
        "faculty_code": faculty.code if faculty else None,
        "force_password_change": current_user.force_password_change,
        "token_version": current_user.token_version,
        "is_deleted": current_user.is_deleted,
        "created_at": current_user.created_at.isoformat() if current_user.created_at else None,
        "updated_at": current_user.updated_at.isoformat() if current_user.updated_at else None,
    }


@router.put("/api/profile")
async def update_profile(
    payload: Dict[str, Any] = Body(...),
    current_user: User = Depends(get_vd),
    db: Session = Depends(get_db),
):
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
    _log(db, current_user.id, "PROFILE_UPDATED", "users", current_user.id)
    db.commit()
    return {"ok": True, "full_name": current_user.full_name}


# ─── EXPORT ───────────────────────────────────────────────────────────────────

@router.get("/api/export/students")
async def export_students(
    current_user: User = Depends(get_vd),
    db: Session = Depends(get_db),
):
    fid = current_user.faculty_id
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["#", "Ном", "Рамз", "Гурӯҳ", "Курс", "NB", "Вилоят", "Зодгоҳ", "Тел. волидайн"])
    for i, s in enumerate(
        db.query(Student)
        .join(Group)
        .filter(Group.faculty_id == fid, Student.is_deleted == False)
        .options(joinedload(Student.group).joinedload(Group.course))
        .order_by(Student.full_name)
        .all(),
        1,
    ):
        w.writerow(
            [
                i,
                s.full_name,
                s.student_code or "",
                s.group.number if s.group else "",
                s.group.course.year if s.group and s.group.course else "",
                int(s.total_absent_hours or 0),
                s.region or "",
                s.birth_place or "",
                s.parent_phone or "",
            ]
        )
    return Response(
        content=out.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=students_{date.today()}.csv"
        },
    )


@router.get("/api/export/nb")
async def export_nb(
    current_user: User = Depends(get_vd),
    db: Session = Depends(get_db),
):
    fid = current_user.faculty_id
    nb_limit = int(get_system_setting(db, "NB_LIMIT_HIGH", "35"))
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["#", "Ном", "Гурӯҳ", "Курс", "NB умумӣ", "Тел. волидайн"])
    for i, s in enumerate(
        db.query(Student)
        .join(Group)
        .filter(
            Group.faculty_id == fid,
            Student.is_deleted == False,
            Student.total_absent_hours >= nb_limit,
        )
        .options(joinedload(Student.group).joinedload(Group.course))
        .order_by(Student.total_absent_hours.desc())
        .all(),
        1,
    ):
        w.writerow(
            [
                i,
                s.full_name,
                s.group.number if s.group else "",
                s.group.course.year if s.group and s.group.course else "",
                int(s.total_absent_hours or 0),
                s.parent_phone or "",
            ]
        )
    return Response(
        content=out.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=nb_{date.today()}.csv"},
    )


# ─── CHANGE PASSWORD ──────────────────────────────────────────────────────────

@router.get("/change-password", response_class=HTMLResponse)
async def change_password_page(
    request: Request, current_user: User = Depends(get_vd)
):
    return templates.TemplateResponse(
        "change_password.html", {"request": request, "user": current_user}
    )


@router.post("/change-password", response_class=HTMLResponse)
async def change_password_post(
    request: Request,
    current_user: User = Depends(get_vd),
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
    _log(db, current_user.id, "PASSWORD_CHANGED", "users", current_user.id)
    db.commit()
    token = create_access_token(
        {
            "sub": str(current_user.id),
            "ver": current_user.token_version,
            "role": current_user.role.value,
        }
    )
    response = RedirectResponse("/vice-dean/dashboard", status_code=303)
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