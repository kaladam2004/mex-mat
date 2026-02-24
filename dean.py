"""
dean.py — OPTIMIZED VERSION for Supabase/PostgreSQL
Dean: daily attendance control, weekly view, group/curator management
Performance: batch queries, no N+1 problems
"""
from __future__ import annotations

import calendar as _calendar
import csv
import io
from datetime import date, timedelta, datetime
from typing import Optional, List, Dict, Any, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request, Form, Query, Body
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session, joinedload, contains_eager
from sqlalchemy import func, and_, case

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
    """Fast: returns IDs of all non-deleted groups for faculty."""
    return [gid for (gid,) in db.query(Group.id).filter(
        Group.faculty_id == faculty_id, Group.is_deleted == False
    ).all()]


def _active_group_ids(db: Session, faculty_id: int) -> List[int]:
    """Active groups — treat NULL is_active as active."""
    return [gid for (gid,) in db.query(Group.id).filter(
        Group.faculty_id == faculty_id,
        Group.is_deleted == False,
        Group.is_active != False,        # NULL and True both pass
    ).all()]


def _week_bounds(target: date = None):
    if target is None:
        target = date.today()
    monday = target - timedelta(days=target.weekday())
    saturday = monday + timedelta(days=5)
    return monday, saturday


def _batch_attendance_for_date(db: Session, group_ids: List[int], target: date) -> Dict[int, Dict]:
    """
    Returns per-group attendance info for a specific date.
    Result: {group_id: {lesson_id, marked, total_students, status}}
    One query per data type — no N+1.
    """
    if not group_ids:
        return {}

    # 1) Lessons for these groups on this date
    lessons = db.query(Lesson.id, Lesson.group_id).filter(
        Lesson.group_id.in_(group_ids),
        Lesson.lesson_date == target,
    ).all()
    lesson_map = {gid: lid for lid, gid in lessons}   # group_id -> lesson_id

    # 2) Student counts per group (one query)
    student_counts_raw = db.query(
        Student.group_id, func.count(Student.id)
    ).filter(
        Student.group_id.in_(group_ids),
        Student.is_deleted == False,
    ).group_by(Student.group_id).all()
    student_counts = {gid: cnt for gid, cnt in student_counts_raw}

    # 3) Attendance counts per lesson (one query)
    lesson_ids = list(lesson_map.values())
    att_counts: Dict[int, int] = {}
    if lesson_ids:
        att_counts_raw = db.query(
            Attendance.lesson_id, func.count(Attendance.id)
        ).filter(Attendance.lesson_id.in_(lesson_ids)).group_by(Attendance.lesson_id).all()
        att_counts = {lid: cnt for lid, cnt in att_counts_raw}

    result: Dict[int, Dict] = {}
    for gid in group_ids:
        total = student_counts.get(gid, 0)
        lid = lesson_map.get(gid)
        marked = att_counts.get(lid, 0) if lid else 0
        if not lid or marked == 0:
            status = "NOT_STARTED"
        elif marked >= total:
            status = "COMPLETED"
        else:
            status = "IN_PROGRESS"
        result[gid] = {
            "lesson_id": lid,
            "marked": marked,
            "total_students": total,
            "status": status,
        }
    return result


def _attendance_pct_for_date(db: Session, faculty_id: int, target: date) -> Tuple[float, int, int]:
    """Returns (pct, present, recorded) for the whole faculty on a date."""
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
    present = int(counts.get("present", 0) or 0)
    absent  = int(counts.get("absent",  0) or 0)
    recorded = present + absent
    pct = round(present / recorded * 100.0, 1) if recorded else 0.0
    return pct, present, recorded


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


# ─── API: STATS (main — dashboard + analytics) ───────────────────────────────

@router.get("/api/stats")
async def stats(
    current_user: User = Depends(get_current_dean),
    db: Session = Depends(get_db),
):
    fid = current_user.faculty_id
    nb_limit = int(get_system_setting(db, "NB_LIMIT_HIGH", "35"))
    today = date.today()

    # ── Basic counts (3 queries) ──────────────────────────────────────────
    total_students = db.query(func.count(Student.id)).join(Group).filter(
        Group.faculty_id == fid, Student.is_deleted == False,
    ).scalar() or 0

    total_groups = db.query(func.count(Group.id)).filter(
        Group.faculty_id == fid, Group.is_deleted == False,
    ).scalar() or 0

    total_curators = db.query(func.count(User.id)).filter(
        User.faculty_id == fid, User.role == UserRole.CURATOR, User.is_deleted == False,
    ).scalar() or 0

    high_absence_count = db.query(func.count(Student.id)).join(Group).filter(
        Group.faculty_id == fid, Student.is_deleted == False,
        Student.total_absent_hours >= nb_limit,
    ).scalar() or 0

    att_pct, present_today, recorded_today = _attendance_pct_for_date(db, fid, today)

    # ── Shift stats (batch) ───────────────────────────────────────────────
    shift_data = db.query(
        Group.shift,
        func.count(Student.id).label("total_students"),
    ).join(Student, and_(Student.group_id == Group.id, Student.is_deleted == False)
    ).filter(Group.faculty_id == fid, Group.is_deleted == False
    ).group_by(Group.shift).all()
    shift_map = {row.shift: row.total_students for row in shift_data}
    group_shift_counts = db.query(Group.shift, func.count(Group.id)).filter(
        Group.faculty_id == fid, Group.is_deleted == False
    ).group_by(Group.shift).all()
    gshift_map = {s: c for s, c in group_shift_counts}

    shift_stats = {
        "shift1": {"attendance_rate": att_pct, "total_students": shift_map.get(1, 0), "groups": gshift_map.get(1, 0)},
        "shift2": {"attendance_rate": att_pct, "total_students": shift_map.get(2, 0), "groups": gshift_map.get(2, 0)},
    }

    # ── Course stats (batch) ──────────────────────────────────────────────
    course_rows = db.query(
        Course.year,
        func.count(Group.id).label("grp_count"),
        func.count(Student.id).label("stu_count"),
    ).join(Group, Group.course_id == Course.id
    ).join(Student, and_(Student.group_id == Group.id, Student.is_deleted == False)
    ).filter(Group.faculty_id == fid, Group.is_deleted == False
    ).group_by(Course.year).order_by(Course.year).all()
    course_stats = [
        {"course_year": row.year, "total_students": row.stu_count,
         "groups": row.grp_count, "attendance_rate": att_pct}
        for row in course_rows if row.stu_count > 0
    ]

    # ── Top/Bottom groups by real attendance % (last 30 days) ────────────
    # For each group: attendance_pct = present / (present+absent) * 100
    thirty_ago = today - timedelta(days=30)
    group_ids_all = _active_group_ids(db, fid)

    group_att: Dict[int, Dict] = {}
    if group_ids_all:
        lesson_rows = db.query(Lesson.id, Lesson.group_id).filter(
            Lesson.group_id.in_(group_ids_all),
            Lesson.lesson_date >= thirty_ago,
        ).all()
        l_ids = [r.id for r in lesson_rows]
        l_to_g = {r.id: r.group_id for r in lesson_rows}

        if l_ids:
            att_rows = db.query(
                Attendance.lesson_id,
                Attendance.status,
                func.count(Attendance.id).label("cnt"),
            ).filter(Attendance.lesson_id.in_(l_ids)
            ).group_by(Attendance.lesson_id, Attendance.status).all()

            per_group: Dict[int, Dict[str, int]] = {}
            for row in att_rows:
                gid2 = l_to_g[row.lesson_id]
                per_group.setdefault(gid2, {})
                per_group[gid2][row.status] = per_group[gid2].get(row.status, 0) + row.cnt
            for gid2, sc in per_group.items():
                p = sc.get("present", 0)
                a = sc.get("absent", 0)
                total = p + a
                group_att[gid2] = {
                    "attendance_rate": round(p / total * 100, 1) if total else 0.0,
                }

    # Fetch group numbers (one query)
    grp_rows = db.query(Group.id, Group.number).filter(
        Group.id.in_(group_ids_all)
    ).all() if group_ids_all else []
    grp_num = {r.id: r.number for r in grp_rows}

    group_list = [
        {"number": grp_num[gid], "attendance_rate": group_att.get(gid, {}).get("attendance_rate", 0)}
        for gid in group_ids_all if gid in grp_num
    ]
    group_list.sort(key=lambda x: x["attendance_rate"], reverse=True)
    top_groups = group_list[:5]
    bottom_groups = sorted(group_list, key=lambda x: x["attendance_rate"])[:5]

    return {
        "total_students": total_students,
        "total_groups": total_groups,
        "total_curators": total_curators,
        "high_absence_count": high_absence_count,
        "nb_limit": nb_limit,
        "attendance_rate": att_pct,
        "attendance_today": {"pct": att_pct, "present": present_today, "recorded": recorded_today},
        "shift_stats": shift_stats,
        "course_stats": course_stats,
        "top_groups": top_groups,
        "bottom_groups": bottom_groups,
    }


# ─── API: ATTENDANCE CHART DATA ───────────────────────────────────────────────

@router.get("/api/attendance")
async def attendance_chart(
    mode: str = Query("daily", description="daily | weekly | monthly"),
    current_user: User = Depends(get_current_dean),
    db: Session = Depends(get_db),
):
    """Returns labels + values for attendance chart (optimized)."""
    fid = current_user.faculty_id
    today = date.today()
    group_ids = _faculty_group_ids(db, fid)
    labels: List[str] = []
    values: List[float] = []

    if not group_ids:
        return {"labels": [], "values": [], "mode": mode}

    def _range_att(start: date, end: date) -> float:
        """Average attendance % for a date range (2 queries total)."""
        lids = [lid for (lid,) in db.query(Lesson.id).filter(
            Lesson.group_id.in_(group_ids),
            Lesson.lesson_date >= start,
            Lesson.lesson_date <= end,
        ).all()]
        if not lids:
            return 0.0
        counts = dict(db.query(Attendance.status, func.count(Attendance.id)).filter(
            Attendance.lesson_id.in_(lids)
        ).group_by(Attendance.status).all())
        p = int(counts.get("present", 0) or 0)
        a = int(counts.get("absent",  0) or 0)
        rec = p + a
        return round(p / rec * 100.0, 1) if rec else 0.0

    if mode == "daily":
        for i in range(13, -1, -1):
            d = today - timedelta(days=i)
            pct = _range_att(d, d)
            labels.append(f"{d.day}/{d.month}")
            values.append(pct)

    elif mode == "weekly":
        monday, _ = _week_bounds(today)
        for i in range(7, -1, -1):
            w_mon = monday - timedelta(weeks=i)
            w_sat = w_mon + timedelta(days=5)
            pct = _range_att(w_mon, w_sat)
            labels.append(f"{w_mon.day}/{w_mon.month}")
            values.append(pct)

    else:  # monthly
        month_names = ["Янв", "Фев", "Мар", "Апр", "Май", "Июн",
                       "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]
        for i in range(5, -1, -1):
            year = today.year
            month = today.month - i
            while month <= 0:
                month += 12
                year -= 1
            _, days_in = _calendar.monthrange(year, month)
            pct = _range_att(date(year, month, 1), date(year, month, days_in))
            labels.append(month_names[month - 1])
            values.append(pct)

    return {"labels": labels, "values": values, "mode": mode}


# ─── API: ALERTS ──────────────────────────────────────────────────────────────

@router.get("/api/alerts")
async def get_alerts(
    current_user: User = Depends(get_current_dean),
    db: Session = Depends(get_db),
):
    fid = current_user.faculty_id
    nb_limit = int(get_system_setting(db, "NB_LIMIT_HIGH", "35"))
    today = date.today()
    alerts = []

    # Groups with no lesson today (batch)
    active_ids = _active_group_ids(db, fid)
    if active_ids:
        filled_ids = {gid for (gid,) in db.query(Lesson.group_id).filter(
            Lesson.group_id.in_(active_ids),
            Lesson.lesson_date == today,
        ).all()}
        not_filled = [gid for gid in active_ids if gid not in filled_ids]
        if not_filled:
            grp_nums = {gid: num for gid, num in db.query(Group.id, Group.number).filter(
                Group.id.in_(not_filled)
            ).all()}
            for gid in not_filled[:10]:
                alerts.append({
                    "alert_type": "WARNING",
                    "message": f"Гурӯҳ {grp_nums.get(gid, gid)}: журнал имрӯз пур нашудааст",
                    "created_at": datetime.utcnow().isoformat(),
                })

    # High NB students
    high_nb = db.query(Student.full_name, Student.total_absent_hours).join(Group).filter(
        Group.faculty_id == fid, Student.is_deleted == False,
        Student.total_absent_hours >= nb_limit,
    ).order_by(Student.total_absent_hours.desc()).limit(5).all()
    for name, hrs in high_nb:
        alerts.append({
            "alert_type": "HIGH_ABSENCE",
            "message": f"{name} — {int(hrs or 0)} соат ғайбуд (НБ)",
            "created_at": datetime.utcnow().isoformat(),
        })

    return alerts[:20]


# ─── API: WEEKLY STATS ────────────────────────────────────────────────────────

@router.get("/api/weekly-stats")
async def weekly_stats(
    current_user: User = Depends(get_current_dean),
    db: Session = Depends(get_db),
):
    fid = current_user.faculty_id
    today = date.today()
    monday, _ = _week_bounds(today)
    group_ids = _faculty_group_ids(db, fid)
    result = []
    for i in range(7, -1, -1):
        w_mon = monday - timedelta(weeks=i)
        w_sat = w_mon + timedelta(days=5)
        if not group_ids:
            avg_nb = 0
        else:
            lids = [lid for (lid,) in db.query(Lesson.id).filter(
                Lesson.group_id.in_(group_ids),
                Lesson.lesson_date >= w_mon,
                Lesson.lesson_date <= w_sat,
            ).all()]
            if lids:
                counts = dict(db.query(Attendance.status, func.count(Attendance.id)).filter(
                    Attendance.lesson_id.in_(lids)
                ).group_by(Attendance.status).all())
                ab = int(counts.get("absent", 0) or 0)
                total = sum(counts.values())
                avg_nb = round(ab / max(total, 1) * 100, 1)
            else:
                avg_nb = 0
        result.append({
            "week": f"{w_mon.day}/{w_mon.month}–{w_sat.day}/{w_sat.month}",
            "avg_nb": avg_nb,
        })
    return result


# ─── API: AT-RISK ─────────────────────────────────────────────────────────────

@router.get("/api/at-risk")
async def at_risk(
    group_id: Optional[int] = Query(None),
    course: Optional[int] = Query(None),
    current_user: User = Depends(get_current_dean),
    db: Session = Depends(get_db),
):
    fid = current_user.faculty_id
    nb_limit = int(get_system_setting(db, "NB_LIMIT_HIGH", "35"))
    q = db.query(Student).join(Group, Student.group_id == Group.id).filter(
        Group.faculty_id == fid, Student.is_deleted == False,
        Student.total_absent_hours >= nb_limit,
    )
    if group_id:
        q = q.filter(Student.group_id == group_id)
    if course:
        q = q.filter(Group.course_id.in_(
            db.query(Course.id).filter(Course.year == course)
        ))
    students = q.options(
        joinedload(Student.group).joinedload(Group.course)
    ).order_by(Student.total_absent_hours.desc()).all()
    return [{
        "id": s.id, "full_name": s.full_name, "student_code": s.student_code,
        "group_number": s.group.number if s.group else None,
        "group_id": s.group_id,
        "total_absent_hours": int(s.total_absent_hours or 0),
        "parent_phone": s.parent_phone,
    } for s in students]


# ─── API: AUDIT LOG ───────────────────────────────────────────────────────────

@router.get("/api/audit-log")
async def audit_log(
    action: Optional[str] = Query(None),
    target_date: Optional[str] = Query(None, alias="date"),
    current_user: User = Depends(get_current_dean),
    db: Session = Depends(get_db),
):
    q = db.query(AuditLog).filter(AuditLog.user_id == current_user.id)
    if action:
        q = q.filter(AuditLog.action == action)
    if target_date:
        try:
            d = date.fromisoformat(target_date)
            q = q.filter(func.date(AuditLog.created_at) == d)
        except ValueError:
            pass
    logs = q.order_by(AuditLog.id.desc()).limit(100).all()
    return [{
        "id": l.id,
        "user_name": current_user.full_name,
        "action": l.action,
        "object_type": l.target_table,
        "details": l.description or "",
        
    } for l in logs]


# ─── API: OVERVIEW STATS (legacy) ─────────────────────────────────────────────

@router.get("/api/stats/overview")
async def stats_overview(
    current_user: User = Depends(get_current_dean),
    db: Session = Depends(get_db),
):
    fid = current_user.faculty_id
    nb_limit = int(get_system_setting(db, "NB_LIMIT_HIGH", "35"))
    today = date.today()
    total_students = db.query(func.count(Student.id)).join(Group).filter(
        Group.faculty_id == fid, Student.is_deleted == False,
    ).scalar() or 0
    total_groups = db.query(func.count(Group.id)).filter(
        Group.faculty_id == fid, Group.is_deleted == False,
    ).scalar() or 0
    total_curators = db.query(func.count(User.id)).filter(
        User.faculty_id == fid, User.role == UserRole.CURATOR, User.is_deleted == False,
    ).scalar() or 0
    high_nb_count = db.query(func.count(Student.id)).join(Group).filter(
        Group.faculty_id == fid, Student.is_deleted == False,
        Student.total_absent_hours >= nb_limit,
    ).scalar() or 0
    att_today_pct, present_today, recorded_today = _attendance_pct_for_date(db, fid, today)
    return {
        "total_students": total_students, "total_groups": total_groups,
        "total_curators": total_curators, "high_nb_count": high_nb_count,
        "nb_limit": nb_limit,
        "attendance_today": {"pct": att_today_pct, "present": present_today, "recorded": recorded_today},
    }


# ─── API: DAILY ATTENDANCE CONTROL ────────────────────────────────────────────

@router.get("/api/daily-control")
async def daily_control(
    target_date: Optional[str] = Query(None),
    current_user: User = Depends(get_current_dean),
    db: Session = Depends(get_db),
):
    fid = current_user.faculty_id
    d = date.fromisoformat(target_date) if target_date else date.today()

    # All active groups (NULL is_active treated as active)
    groups = db.query(Group).filter(
        Group.faculty_id == fid,
        Group.is_deleted == False,
        Group.is_active != False,       # include NULL and True
    ).options(
        joinedload(Group.course),
    ).order_by(Group.number).all()

    if not groups:
        return {"date": str(d), "summary": {"total_groups": 0, "completed": 0, "in_progress": 0, "not_started": 0}, "groups": []}

    group_ids = [g.id for g in groups]

    # Batch: all attendance data for this date
    att_data = _batch_attendance_for_date(db, group_ids, d)

    # Batch: curator names (one query)
    curator_ids = list({g.curator_id for g in groups if g.curator_id})
    curators_map: Dict[int, str] = {}
    if curator_ids:
        for uid, fname in db.query(User.id, User.full_name).filter(User.id.in_(curator_ids)).all():
            curators_map[uid] = fname

    result = []
    for g in groups:
        info = att_data.get(g.id, {"lesson_id": None, "marked": 0, "total_students": 0, "status": "NOT_STARTED"})
        total_s = info["total_students"]
        marked = info["marked"]
        pct = round(marked / total_s * 100) if total_s else 0
        result.append({
            "group_id": g.id,
            "group_number": g.number,
            "shift": g.shift,
            "course_year": g.course.year if g.course else None,
            "curator_name": curators_map.get(g.curator_id) if g.curator_id else None,
            "total_students": total_s,
            "marked": marked,
            "marked_count": marked,
            "completion_percentage": pct,
            "status": info["status"],
            "lesson_id": info["lesson_id"],
        })

    done    = sum(1 for r in result if r["status"] == "COMPLETED")
    partial = sum(1 for r in result if r["status"] == "IN_PROGRESS")
    none    = sum(1 for r in result if r["status"] == "NOT_STARTED")

    return {
        "date": str(d),
        "summary": {"total_groups": len(result), "completed": done, "in_progress": partial, "not_started": none},
        "groups": result,
    }


# ─── API: WEEKLY CONTROL ──────────────────────────────────────────────────────

@router.get("/api/weekly-control")
async def weekly_control(
    week_start: Optional[str] = Query(None),
    current_user: User = Depends(get_current_dean),
    db: Session = Depends(get_db),
):
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
        Group.faculty_id == fid,
        Group.is_deleted == False,
        Group.is_active != False,
    ).options(joinedload(Group.course)).order_by(Group.number).all()

    if not groups:
        return {"week_start": str(monday), "week_end": str(saturday),
                "days": [str(d) for d in days], "groups": []}

    group_ids = [g.id for g in groups]

    # Batch all lessons for the week
    all_lessons = db.query(Lesson.id, Lesson.group_id, Lesson.lesson_date).filter(
        Lesson.group_id.in_(group_ids),
        Lesson.lesson_date >= monday,
        Lesson.lesson_date <= saturday,
    ).all()
    lesson_map: Dict[tuple, int] = {}
    for lid, gid, ldate in all_lessons:
        lesson_map[(gid, ldate)] = lid

    # Batch attendance counts
    lesson_ids_week = [lid for lid, gid, ldate in all_lessons]
    att_counts: Dict[int, int] = {}
    if lesson_ids_week:
        att_counts = {lid: cnt for lid, cnt in db.query(
            Attendance.lesson_id, func.count(Attendance.id)
        ).filter(Attendance.lesson_id.in_(lesson_ids_week)
        ).group_by(Attendance.lesson_id).all()}

    # Batch student counts
    student_counts = {gid: cnt for gid, cnt in db.query(
        Student.group_id, func.count(Student.id)
    ).filter(
        Student.group_id.in_(group_ids), Student.is_deleted == False
    ).group_by(Student.group_id).all()}

    # Batch curators
    curator_ids = list({g.curator_id for g in groups if g.curator_id})
    curators_map: Dict[int, str] = {}
    if curator_ids:
        for uid, fname in db.query(User.id, User.full_name).filter(User.id.in_(curator_ids)).all():
            curators_map[uid] = fname

    result = []
    for g in groups:
        total_s = student_counts.get(g.id, 0)
        days_status: Dict[str, Dict] = {}
        missing_days: List[str] = []
        completed_count = 0

        for d in days:
            lid = lesson_map.get((g.id, d))
            if not lid:
                status = "NOT_STARTED"
                marked = 0
            else:
                marked = att_counts.get(lid, 0)
                if marked == 0:
                    status = "NOT_STARTED"
                elif marked >= total_s:
                    status = "COMPLETED"
                    completed_count += 1
                else:
                    status = "IN_PROGRESS"
            days_status[str(d)] = {"status": status, "marked": marked, "total": total_s}
            if status != "COMPLETED":
                missing_days.append(str(d))

        result.append({
            "group_id": g.id,
            "group_number": g.number,
            "shift": g.shift,
            "course_year": g.course.year if g.course else None,
            "curator_name": curators_map.get(g.curator_id) if g.curator_id else None,
            "total_students": total_s,
            "days": days_status,
            "missing_days": missing_days,
            "completion_pct": round(completed_count / 6 * 100),
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
    ).options(
        joinedload(Group.course),
        joinedload(Group.curator),
    ).order_by(Group.number).all()

    # Batch student counts
    group_ids = [g.id for g in groups]
    student_counts: Dict[int, int] = {}
    if group_ids:
        student_counts = {gid: cnt for gid, cnt in db.query(
            Student.group_id, func.count(Student.id)
        ).filter(
            Student.group_id.in_(group_ids), Student.is_deleted == False
        ).group_by(Student.group_id).all()}

    return [{
        "id": g.id, "number": g.number, "shift": g.shift,
        "course_year": g.course.year if g.course else None,
        "course_id": g.course_id,
        "curator": g.curator.full_name if g.curator else None,
        "curator_name": g.curator.full_name if g.curator else None,
        "curator_id": g.curator_id,
        "total_students": student_counts.get(g.id, 0),
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
    g.number = number
    g.shift = shift
    g.course_id = course_id
    g.curator_id = curator_id
    if curator_id:
        g.is_active = True   # automatically activate when curator is assigned
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
            g.is_active = True   # automatically activate when curator is assigned
        else:
            g.curator_id = None
    if "is_active" in payload:
        g.is_active = bool(payload["is_active"])
    _audit(db, current_user.id, "GROUP_PATCHED", "groups", gid, str(payload))
    db.commit()
    return {"ok": True}


@router.post("/api/groups/{gid}/assign-curator")
async def assign_curator_to_group(
    gid: int,
    payload: Dict[str, Any] = Body(...),
    current_user: User = Depends(get_current_dean),
    db: Session = Depends(get_db),
):
    """Quick endpoint: assign or unassign curator from a group. Auto-sets is_active=True."""
    g = db.query(Group).filter(
        Group.id == gid,
        Group.faculty_id == current_user.faculty_id,
        Group.is_deleted == False,
    ).first()
    if not g:
        raise HTTPException(404, "Гурӯҳ ёфт нашуд")

    cid = payload.get("curator_id")
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
        _audit(db, current_user.id, "CURATOR_ASSIGNED", "groups", gid,
               f"{c.full_name} → {g.number}")
    else:
        old_cid = g.curator_id
        g.curator_id = None
        _audit(db, current_user.id, "CURATOR_REMOVED", "groups", gid, g.number)

    db.commit()
    return {"ok": True, "group_id": gid, "curator_id": cid, "is_active": g.is_active}


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
    course: Optional[int] = Query(None),
    high_nb: Optional[bool] = Query(None),
    birth_place: Optional[str] = Query(None),
    region: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    current_user: User = Depends(get_current_dean),
    db: Session = Depends(get_db),
):
    fid = current_user.faculty_id
    nb_limit = int(get_system_setting(db, "NB_LIMIT_HIGH", "35"))
    q = db.query(Student).join(Group, Student.group_id == Group.id).filter(
        Group.faculty_id == fid, Student.is_deleted == False
    ).options(
        joinedload(Student.group).joinedload(Group.course),
    )
    if group_id:
        q = q.filter(Student.group_id == group_id)
    if search and len(search) >= 2:
        q = q.filter(Student.full_name.ilike(f"%{search}%"))
    if course_year:
        q = q.filter(Group.course_id.in_(
            db.query(Course.id).filter(Course.year == course_year)
        ))
    if course:
        q = q.filter(Group.course_id.in_(
            db.query(Course.id).filter(Course.year == course)
        ))
    if high_nb:
        q = q.filter(Student.total_absent_hours >= nb_limit)
    if birth_place and len(birth_place) >= 2:
        q = q.filter(Student.birth_place.ilike(f"%{birth_place}%"))
    if region and len(region) >= 2:
        q = q.filter(Student.region.ilike(f"%{region}%"))

    total = q.count()
    students = q.order_by(Student.full_name).offset((page - 1) * page_size).limit(page_size).all()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "nb_limit": nb_limit,
        "students": [{
            "id": s.id, "full_name": s.full_name, "student_code": s.student_code,
            "group_number": s.group.number if s.group else None,
            "group_id": s.group_id,
            "course_year": s.group.course.year if s.group and s.group.course else None,
            "total_absences": int(s.total_absent_hours or 0),
            "total_absent_hours": int(s.total_absent_hours or 0),
            "birth_place": s.birth_place or "",
            "region": s.region or "",
            "parent_phone": s.parent_phone or "",
            "is_high_risk": (s.total_absent_hours or 0) >= nb_limit,
        } for s in students],
    }


@router.get("/api/students/{sid}")
async def get_student(
    sid: int,
    current_user: User = Depends(get_current_dean),
    db: Session = Depends(get_db),
):
    fid = current_user.faculty_id
    s = db.query(Student).join(Group).filter(
        Student.id == sid, Group.faculty_id == fid, Student.is_deleted == False
    ).options(joinedload(Student.group).joinedload(Group.course)).first()
    if not s:
        raise HTTPException(404, "Донишҷӯ ёфт нашуд")
    nb_limit = int(get_system_setting(db, "NB_LIMIT_HIGH", "35"))
    return {
        "id": s.id, "full_name": s.full_name, "student_code": s.student_code,
        "birth_year": s.birth_year, "birth_place": s.birth_place,
        "region": s.region, "parent_phone": s.parent_phone,
        "study_start": str(s.study_start) if s.study_start else None,
        "total_absent_hours": int(s.total_absent_hours or 0),
        "group_number": s.group.number if s.group else None,
        "group_id": s.group_id,
        "course_year": s.group.course.year if s.group and s.group.course else None,
        "is_high_risk": (s.total_absent_hours or 0) >= nb_limit,
    }


@router.get("/api/students/{sid}/attendance")
async def student_attendance(
    sid: int,
    current_user: User = Depends(get_current_dean),
    db: Session = Depends(get_db),
):
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
        "student": {"id": s.id, "full_name": s.full_name, "total_absent_hours": int(s.total_absent_hours or 0)},
        "records": [
            {"date": str(l.lesson_date), "status": a.status, "nb_hours": a.nb_hours,
             "comment": a.comment or "", "is_reasoned": a.is_reasoned}
            for a, l in records
        ],
    }


@router.post("/api/students")
async def create_student(
    full_name: str = Form(...),
    student_code: str = Form(""),
    group_id: int = Form(...),
    birth_year: Optional[int] = Form(None),
    birth_place: Optional[str] = Form(None),
    phone: Optional[str] = Form(None),
    parent_phone: Optional[str] = Form(None),
    current_user: User = Depends(get_current_dean),
    db: Session = Depends(get_db),
):
    fid = current_user.faculty_id
    g = db.query(Group).filter(
        Group.id == group_id, Group.faculty_id == fid, Group.is_deleted == False
    ).first()
    if not g:
        raise HTTPException(400, "Гурӯҳ ёфт нашуд")
    s = Student(
        full_name=full_name,
        student_code=student_code or None,
        group_id=group_id,
        birth_year=birth_year,
        birth_place=birth_place,
        phone=phone,
        parent_phone=parent_phone,
        total_absent_hours=0,
        is_deleted=False,
    )
    db.add(s)
    db.commit()
    _audit(db, current_user.id, "STUDENT_CREATED", "students", s.id, full_name)
    db.commit()
    return {"id": s.id, "full_name": s.full_name}


@router.put("/api/students/{sid}")
async def update_student(
    sid: int,
    full_name: str = Form(...),
    student_code: str = Form(""),
    group_id: int = Form(...),
    birth_year: Optional[int] = Form(None),
    birth_place: Optional[str] = Form(None),
    phone: Optional[str] = Form(None),
    parent_phone: Optional[str] = Form(None),
    current_user: User = Depends(get_current_dean),
    db: Session = Depends(get_db),
):
    fid = current_user.faculty_id
    s = db.query(Student).join(Group).filter(
        Student.id == sid, Group.faculty_id == fid, Student.is_deleted == False
    ).first()
    if not s:
        raise HTTPException(404, "Донишҷӯ ёфт нашуд")
    s.full_name = full_name
    s.student_code = student_code or None
    s.group_id = group_id
    s.birth_year = birth_year
    s.birth_place = birth_place
    s.phone = phone
    s.parent_phone = parent_phone
    db.commit()
    _audit(db, current_user.id, "STUDENT_UPDATED", "students", sid, full_name)
    db.commit()
    return {"ok": True}


@router.delete("/api/students/{sid}")
async def delete_student(
    sid: int,
    current_user: User = Depends(get_current_dean),
    db: Session = Depends(get_db),
):
    fid = current_user.faculty_id
    s = db.query(Student).join(Group).filter(
        Student.id == sid, Group.faculty_id == fid, Student.is_deleted == False
    ).first()
    if not s:
        raise HTTPException(404)
    s.is_deleted = True
    _audit(db, current_user.id, "STUDENT_DELETED", "students", sid, s.full_name)
    db.commit()
    return {"ok": True}


# ─── API: CURATORS ────────────────────────────────────────────────────────────

@router.get("/api/curators")
async def list_curators(
    current_user: User = Depends(get_current_dean),
    db: Session = Depends(get_db),
):
    fid = current_user.faculty_id
    curators = db.query(User).filter(
        User.role == UserRole.CURATOR,
        User.faculty_id == fid,
        User.is_deleted == False,
    ).order_by(User.full_name).all()

    curator_ids = [c.id for c in curators]

    # Batch: get groups for all curators (one query)
    grp_map: Dict[int, Dict] = {}
    if curator_ids:
        grp_rows = db.query(Group.curator_id, Group.id, Group.number).filter(
            Group.curator_id.in_(curator_ids),
            Group.is_deleted == False,
            Group.is_active != False,
        ).all()
        for cid, gid, gnum in grp_rows:
            if cid not in grp_map:           # first group per curator
                grp_map[cid] = {"id": gid, "number": gnum}

    result = []
    for c in curators:
        grp = grp_map.get(c.id)
        result.append({
            "id": c.id, "full_name": c.full_name, "username": c.username,
            "email": c.email, "phone": c.phone, "department": c.department,
            "is_active": not getattr(c, "is_deleted", False),
            "group": grp,
            "group_number": grp["number"] if grp else None,
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


# ─── API: NB-LIST ─────────────────────────────────────────────────────────────

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
    ).options(joinedload(Student.group)).order_by(Student.total_absent_hours.desc()).all()
    return [{
        "id": s.id, "full_name": s.full_name, "student_code": s.student_code,
        "group_number": s.group.number if s.group else None,
        "total_absent_hours": int(s.total_absent_hours or 0),
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
    w.writerow(["ID", "full_name", "student_code", "group", "course", "total_nb", "region", "birth_place", "parent_phone"])
    for s in db.query(Student).join(Group).filter(
        Group.faculty_id == fid, Student.is_deleted == False
    ).options(joinedload(Student.group).joinedload(Group.course)).order_by(Student.full_name).all():
        w.writerow([
            s.id, s.full_name, s.student_code,
            s.group.number if s.group else "",
            s.group.course.year if s.group and s.group.course else "",
            int(s.total_absent_hours or 0),
            s.region or "", s.birth_place or "", s.parent_phone or "",
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
    ).options(joinedload(Student.group).joinedload(Group.course)
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