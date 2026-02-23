from models import SessionLocal, User, UserRole, get_password_hash

def create_rector():
    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.username == "rector").first()
        if existing:
            print("⚠️  Rector already exists")
            return
        rector = User(
            full_name="University Rector",
            username="rector",
            password_hash=get_password_hash("123456"),
            role=UserRole.RECTOR,
            token_version=1,
            force_password_change=True,
        )
        db.add(rector)
        db.commit()
        print("✅ Rector created → username: rector | password: 123456")
    finally:
        db.close()

if __name__ == "__main__":
    create_rector()