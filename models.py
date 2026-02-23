"""
models.py — PostgreSQL / Supabase version
"""
import enum
import os
from datetime import datetime, timezone

from sqlalchemy import (
    create_engine, Column, Integer, String, ForeignKey, Date, Boolean,
    Float, DateTime, Text, UniqueConstraint, Index, Enum, CheckConstraint, BigInteger
)
from sqlalchemy.orm import relationship, declarative_base, sessionmaker
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
import bcrypt

from config import DATABASE_URL

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    connect_args={"sslmode": "require"},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# ─── ENUMS ────────────────────────────────────────────────────────────────────

class UserRole(enum.Enum):
    ADMIN     = "admin"
    RECTOR    = "rector"
    DEAN      = "dean"
    VICE_DEAN = "vice_dean"
    CURATOR   = "curator"


class AlertType(enum.Enum):
    HIGH_NB             = "high_nb"
    CONSECUTIVE_ABSENCE = "consecutive_absence"
    LOW_ATTENDANCE      = "low_attendance"


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def get_password_hash(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


# ─── MIXINS ───────────────────────────────────────────────────────────────────

class TimestampMixin:
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), onupdate=lambda: datetime.now(timezone.utc))


class SoftDeleteMixin:
    is_deleted = Column(Boolean, default=False, nullable=False, index=True)


# ─── TABLES ───────────────────────────────────────────────────────────────────

class AcademicYear(Base):
    __tablename__ = "academic_years"
    id         = Column(Integer, primary_key=True)
    name       = Column(String(20), unique=True, nullable=False)
    start_date = Column(Date, nullable=False)
    end_date   = Column(Date, nullable=False)
    is_current = Column(Boolean, default=False)
    __table_args__ = (CheckConstraint("start_date < end_date"),)


class Faculty(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "faculties"
    id       = Column(Integer, primary_key=True)
    name     = Column(String(150), unique=True, nullable=False)
    code     = Column(String(20), unique=True, nullable=False)
    logo_url = Column(String(500))
    groups = relationship("Group", back_populates="faculty")
    users  = relationship("User", back_populates="faculty")


class Course(Base):
    __tablename__ = "courses"
    id   = Column(Integer, primary_key=True)
    year = Column(Integer, unique=True, nullable=False)


class User(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "users"
    id            = Column(Integer, primary_key=True)
    full_name     = Column(String(150), nullable=False)
    username      = Column(String(100), unique=True, nullable=False, index=True)
    password_hash = Column(Text, nullable=False)
    role          = Column(Enum(UserRole, name="userrole"), nullable=False, index=True)
    faculty_id    = Column(Integer, ForeignKey("faculties.id"), index=True)
    token_version         = Column(Integer, default=1, nullable=False)
    force_password_change = Column(Boolean, default=False)
    birth_year  = Column(Integer)
    department  = Column(String(100))
    email       = Column(String(120))
    phone       = Column(String(20))
    faculty = relationship("Faculty", back_populates="users")


class Group(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "groups"
    id               = Column(Integer, primary_key=True)
    number           = Column(String(50), nullable=False)
    shift            = Column(Integer, nullable=False)
    course_id        = Column(Integer, ForeignKey("courses.id"), nullable=False)
    academic_year_id = Column(Integer, ForeignKey("academic_years.id"), nullable=False)
    faculty_id       = Column(Integer, ForeignKey("faculties.id"), nullable=False)
    curator_id       = Column(Integer, ForeignKey("users.id"))
    is_active        = Column(Boolean, default=True)
    is_closed        = Column(Boolean, default=False)
    faculty       = relationship("Faculty", back_populates="groups")
    course        = relationship("Course")
    academic_year = relationship("AcademicYear")
    curator       = relationship("User", foreign_keys=[curator_id])
    students      = relationship("Student", back_populates="group")
    lessons       = relationship("Lesson", back_populates="group")
    __table_args__ = (CheckConstraint("shift IN (1,2)"),)


class Student(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "students"
    id                  = Column(Integer, primary_key=True)
    student_code        = Column(String(50), unique=True, nullable=False)
    full_name           = Column(String(150), nullable=False)
    faculty_id          = Column(Integer, ForeignKey("faculties.id"), nullable=False)
    group_id            = Column(Integer, ForeignKey("groups.id"), nullable=False)
    birth_year          = Column(Integer)
    birth_place         = Column(String(100))
    region              = Column(String(100))
    parent_phone        = Column(String(30))
    study_start         = Column(Date)
    expected_graduation = Column(Date)
    total_absent_hours  = Column(Integer, default=0)
    faculty    = relationship("Faculty")
    group      = relationship("Group", back_populates="students")
    attendance = relationship("Attendance", back_populates="student", cascade="all, delete-orphan")


class Lesson(Base):
    __tablename__ = "lessons"
    id          = Column(Integer, primary_key=True)
    group_id    = Column(Integer, ForeignKey("groups.id"))
    lesson_date = Column(Date, index=True)
    subject     = Column(String(200))
    lesson_type = Column(String(50))
    group       = relationship("Group", back_populates="lessons")
    attendance  = relationship("Attendance", back_populates="lesson", cascade="all, delete-orphan")


class Attendance(Base, TimestampMixin):
    __tablename__ = "attendance"
    id          = Column(Integer, primary_key=True)
    student_id  = Column(Integer, ForeignKey("students.id"))
    lesson_id   = Column(Integer, ForeignKey("lessons.id"))
    status      = Column(String(10))   # 'present' | 'absent'
    nb_hours    = Column(Integer, default=0)
    comment     = Column(String(500))
    is_reasoned = Column(Boolean, default=False)
    reason_text = Column(Text)
    reasoned_by = Column(Integer, ForeignKey("users.id"))
    marked_by   = Column(Integer, ForeignKey("users.id"))
    student = relationship("Student", back_populates="attendance")
    lesson  = relationship("Lesson", back_populates="attendance")
    __table_args__ = (UniqueConstraint("student_id", "lesson_id"),)


class LoginHistory(Base):
    __tablename__ = "login_history"
    id         = Column(Integer, primary_key=True)
    user_id    = Column(Integer, ForeignKey("users.id"))
    login_time = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    ip_address = Column(String(45))
    user_agent = Column(String(500))
    success    = Column(Boolean, default=True)
    user = relationship("User")


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id           = Column(Integer, primary_key=True)
    user_id      = Column(Integer, ForeignKey("users.id"))
    action       = Column(String(100))
    target_table = Column(String(100))
    target_id    = Column(Integer)
    description  = Column(Text)
    ip_address   = Column(String(45))
    timestamp    = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    user = relationship("User")


class StudentAlert(Base, TimestampMixin):
    __tablename__ = "student_alerts"
    id              = Column(Integer, primary_key=True)
    student_id      = Column(Integer, ForeignKey("students.id"), nullable=True, index=True)
    user_id         = Column(Integer, ForeignKey("users.id"), nullable=True)
    action          = Column(String(100))
    alert_type      = Column(Enum(AlertType, name="alerttype"), nullable=True)
    threshold_value = Column(Integer)
    target_table    = Column(String(100))
    target_id       = Column(Integer)
    triggered_at    = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    is_active       = Column(Boolean, default=True)
    resolved_by     = Column(Integer, ForeignKey("users.id"))
    resolved_at     = Column(DateTime(timezone=True))
    student = relationship("Student", foreign_keys=[student_id])
    actor   = relationship("User", foreign_keys=[user_id])


class Week(Base):
    __tablename__ = "weeks"
    id               = Column(Integer, primary_key=True)
    academic_year_id = Column(Integer, ForeignKey("academic_years.id"), nullable=False, index=True)
    start_date       = Column(Date, nullable=False)
    end_date         = Column(Date, nullable=False)
    week_number      = Column(Integer, nullable=False)
    is_current       = Column(Boolean, default=False)
    academic_year    = relationship("AcademicYear")
    __table_args__ = (Index("idx_week_academic_year_number", "academic_year_id", "week_number", unique=True),)


class DailyFacultyStats(Base):
    __tablename__ = "daily_faculty_stats"
    id               = Column(Integer, primary_key=True)
    faculty_id       = Column(Integer, ForeignKey("faculties.id"), nullable=False, index=True)
    academic_year_id = Column(Integer, ForeignKey("academic_years.id"), nullable=False)
    date             = Column(Date, nullable=False, index=True)
    attendance_rate  = Column(Float, nullable=False)
    total_absents    = Column(Integer, nullable=False)
    __table_args__ = (UniqueConstraint("faculty_id", "academic_year_id", "date"),)


class DailyGroupStats(Base):
    __tablename__ = "daily_group_stats"
    id               = Column(Integer, primary_key=True)
    group_id         = Column(Integer, ForeignKey("groups.id"), nullable=False, index=True)
    academic_year_id = Column(Integer, ForeignKey("academic_years.id"), nullable=False)
    date             = Column(Date, nullable=False, index=True)
    attendance_rate  = Column(Float, nullable=False)
    total_nb_hours   = Column(Integer, nullable=False)
    group = relationship("Group")
    __table_args__ = (UniqueConstraint("group_id", "academic_year_id", "date"),)


class SystemSetting(Base):
    __tablename__ = "system_settings"
    id          = Column(Integer, primary_key=True)
    key         = Column(String(100), unique=True, nullable=False)
    value       = Column(Text, nullable=False)
    description = Column(String(255))


# ─── INIT ─────────────────────────────────────────────────────────────────────

def init_db():
    """Create all tables and seed defaults."""
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        # Admin
        admin = db.query(User).filter(User.username == "admin").first()
        if not admin:
            db.add(User(
                full_name="System Administrator",
                username="admin",
                password_hash=get_password_hash("020304"),
                role=UserRole.ADMIN,
                token_version=1,
                force_password_change=False,
            ))
            db.commit()
            print("✅ Admin created → username: admin | password: 020304")

        # System settings defaults
        defaults = [
            ("NB_LIMIT_HIGH",            "35",  "НБ дараҷаи баланд"),
            ("NB_LIMIT_MEDIUM",          "15",  "НБ дараҷаи миёна"),
            ("CONSECUTIVE_ABSENCE_DAYS", "5",   "Рӯзҳои ғайбати паи ҳам"),
            ("ATTENDANCE_THRESHOLD",     "75",  "Фоизи ҳузури кам"),
        ]
        for key, val, desc in defaults:
            if not db.query(SystemSetting).filter(SystemSetting.key == key).first():
                db.add(SystemSetting(key=key, value=val, description=desc))

        # Courses 1-4
        for yr in [1, 2, 3, 4]:
            if not db.query(Course).filter(Course.year == yr).first():
                db.add(Course(year=yr))

        db.commit()
    finally:
        db.close()