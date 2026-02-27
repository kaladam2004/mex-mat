"""
migrate_nb.py — Барои ҳисоб кардани total_absent_hours барои донишҷӯёни мавҷуда
"""
from sqlalchemy import create_engine, func
from sqlalchemy.orm import Session
from models import Student, Attendance, Base

# Database URL-ро иваз кунед
DATABASE_URL = "postgresql://neondb_owner:npg_RZB0GQEeoSy3@ep-hidden-smoke-a1z8vo7u-pooler.ap-southeast-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require"


engine = create_engine(DATABASE_URL)

with Session(engine) as db:
    students = db.query(Student).filter(
        Student.is_deleted == False,
        Student.total_absent_hours.is_(None)  # Танҳо онҳое, ки NULL доранд
    ).all()
    
    print(f"🔍 {len(students)} донишҷӯ барои ҳисоб кардан ёфт шуд")
    
    updated = 0
    for s in students:
        total = db.query(
            func.coalesce(func.sum(Attendance.nb_hours), 0)
        ).filter(
            Attendance.student_id == s.id,
            Attendance.status == "absent",
        ).scalar() or 0
        
        s.total_absent_hours = int(total)
        updated += 1
        
        if updated % 10 == 0:
            print(f"  ... {updated} донишҷӯ ҳисоб шуд")
    
    db.commit()
    print(f"✅ {updated} донишҷӯ навсозӣ шуд!")