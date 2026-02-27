import os
from fastapi.templating import Jinja2Templates

SECRET_KEY = os.getenv(
    "SECRET_KEY",
    "university-meh-mat-super-secret-key-2024"
)

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 2

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://neondb_owner:npg_RZB0GQEeoSy3@ep-hidden-smoke-a1z8vo7u-pooler.ap-southeast-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require"
)

templates = Jinja2Templates(directory="templates")

COOKIE_NAME = "access_token"
COOKIE_HTTPONLY = True
COOKIE_SECURE = False
COOKIE_SAMESITE = "lax"
COOKIE_PATH = "/"
