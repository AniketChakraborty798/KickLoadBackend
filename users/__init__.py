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
    app.config["JWT_SECRET_KEY"] = os.getenv("JWT_SECRET_KEY")
    app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(minutes=15)  # Short-lived access
    app.config["JWT_REFRESH_TOKEN_EXPIRES"] = timedelta(days=7)     # Refresh for Remember Me
    app.config["JWT_TOKEN_LOCATION"] = ["headers", "cookies"]
    app.config["JWT_COOKIE_SECURE"] = True                          # Only over HTTPS
    app.config["JWT_COOKIE_SAMESITE"] = "Strict"                   # Prevent CSRF from other origins
    app.config["JWT_COOKIE_HTTPONLY"] = True                       # Cannot be accessed via JS
    app.config["JWT_ACCESS_COOKIE_PATH"] = "/"                     # Makes it available to frontend
    app.config["JWT_REFRESH_COOKIE_PATH"] = "/refresh"             # Scoped to refresh route
    app.config["JWT_HEADER_NAME"] = "Authorization"
    app.config["JWT_HEADER_TYPE"] = "Bearer"

    jwt.init_app(app)
