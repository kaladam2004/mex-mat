import bcrypt
import re
from sqlalchemy.orm import Session
from models import SystemSetting


def get_password_hash(pwd: str) -> str:
    return bcrypt.hashpw(pwd.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def validate_password_policy(password: str):
    """6-значный цифровой пароль, не все одинаковые"""
    if not re.fullmatch(r"\d{6}", password):
        return False, "Парол бояд 6 рақам бошад"
    if len(set(password)) == 1:
        return False, "Ҳамаи рақамҳо якхела буда наметавонанд"
    return True, "OK"


def get_system_setting(db: Session, key: str, default: str = "") -> str:
    setting = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    return setting.value if setting else default


def set_system_setting(db: Session, key: str, value: str):
    setting = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    if setting:
        setting.value = value
    else:
        setting = SystemSetting(key=key, value=value)
        db.add(setting)
    db.commit()
    db.refresh(setting)
    return setting