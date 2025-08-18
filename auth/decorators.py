from flask_jwt_extended import verify_jwt_in_request, get_jwt_identity
from functools import wraps
from flask import request, jsonify
from users.models import find_user
from users.models import api_tokens  # if not already imported
from datetime import datetime

def dual_auth_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        token = request.headers.get("X-API-Token")
        if token:
            # Try API token path
            token_doc = api_tokens.find_one({"token": token})
            if not token_doc:
                return jsonify({"error": "Invalid token"}), 401
            if datetime.utcnow() > token_doc["expires_at"]:
                return jsonify({"error": "Token expired"}), 403
            user = find_user(token_doc["email"])
            if not user or user.get("deleted"):
                return jsonify({"error": "User not active"}), 403

            request.api_user = user
            request.token_type = "api"
        else:
            # Try JWT path (frontend login)
            try:
                verify_jwt_in_request()
                identity = get_jwt_identity()
                user = find_user(identity)
                if not user:
                    return jsonify({"error": "User not found"}), 404
                request.api_user = user
                request.token_type = "jwt"
            except Exception as e:
                return jsonify({"error": "Authentication failed", "detail": str(e)}), 401

        return f(*args, **kwargs)
    return wrapper

