# backend/users/__init__.py
from flask_jwt_extended import JWTManager
from datetime import timedelta
import os
from dotenv import load_dotenv
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from redis import Redis

load_dotenv()



redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")

limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=redis_url,  # 👈 use Redis for shared state across workers
    default_limits=[]
)



jwt = JWTManager()  # Expose this if needed in other files

def init_jwt(app):
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "fallback-secret")
    app.config["JWT_SECRET_KEY"] = os.getenv("JWT_SECRET_KEY")
    app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(minutes=15)
    app.config["JWT_REFRESH_TOKEN_EXPIRES"] = timedelta(days=7)

    # ✅ Use only cookies for token storage
    app.config["JWT_TOKEN_LOCATION"] = ["cookies"]

    # ✅ Secure cookie settings
    app.config["JWT_COOKIE_SECURE"] = True
    app.config["JWT_COOKIE_SAMESITE"] = "Strict"
    app.config["JWT_COOKIE_HTTPONLY"] = True

    # ✅ Define cookie paths
    app.config["JWT_ACCESS_COOKIE_PATH"] = "/"
    app.config["JWT_REFRESH_COOKIE_PATH"] = "/refresh"

    # ✅ CSRF protection disabled (since you're not sending CSRF tokens)
    app.config["JWT_COOKIE_CSRF_PROTECT"] = False


    jwt.init_app(app)
