import os
from pymongo import MongoClient
from datetime import datetime, timedelta
from dotenv import load_dotenv
from users.utils import hash_password
from pymongo import ASCENDING


# Load .env variables
load_dotenv()

# Connect to MongoDB
mongo_uri = os.getenv("MONGO_URI")
mongo_db_name = os.getenv("MONGO_DB_NAME")

client = MongoClient(mongo_uri)
db = client[mongo_db_name]
users = db["users"]

try:
    users.create_index("email", unique=True)
except Exception as e:
    print("Failed to create unique index on email:", e)

promo_usage_collection = db["promo_usage"]

# Add at the top
otp_codes = db["otp_codes"]

promo_codes = db["promo_codes"]


# Weekly virtual user tracking for paid users
user_virtual_usage = db["user_virtual_usage"]
user_virtual_usage.create_index("email", unique=True)


# Metrics collection (for tracking per-user stats)
user_metrics = db["user_metrics"]
user_monthly_metrics = db["user_monthly_metrics"]

# Ensure indexes
user_metrics.create_index("email", unique=True)
user_monthly_metrics.create_index([("email", ASCENDING), ("month", ASCENDING)], unique=True)


# Collection for permanent GitHub tokens
github_tokens = db["github_tokens"]
github_tokens.create_index("user_id", unique=True)

# Collection for temporary state storage
temp_states = db["github_temp_states"]
temp_states.create_index("state", expireAfterSeconds=300)  # 5 minutes expiry


# models/github_job_configs.py
github_job_configs = db["github_job_configs"]
github_job_configs.create_index([("repo_url", 1), ("branch", 1)], unique=True)

def save_job_config(repo_url, branch, user_id, jmx_folder, jmx_files):
    github_job_configs.update_one(
        {"repo_url": repo_url, "branch": branch},
        {"$set": {
            "user_id": user_id,
            "jmx_folder": jmx_folder,
            "jmx_files": jmx_files,
            "updated_at": datetime.utcnow()
        }},
        upsert=True
    )

def get_job_config(repo_url, branch):
    return github_job_configs.find_one({"repo_url": repo_url, "branch": branch})




def delete_temp_state(user_id):
    temp_states.delete_many({"user_id": user_id})


def save_temp_state(state, user_id):
    temp_states.insert_one({
        "state": state,
        "user_id": user_id,
        "created_at": datetime.utcnow()
    })

def get_userid_from_state(state):
    doc = temp_states.find_one({"state": state})
    return doc["user_id"] if doc else None

def save_github_token(user_id, token_data):
    github_tokens.update_one(
        {"user_id": user_id},
        {"$set": token_data},
        upsert=True
    )

def get_github_token(user_id):
    return github_tokens.find_one({"user_id": user_id})

def delete_github_token(user_id):
    github_tokens.delete_one({"user_id": user_id})






def get_remaining_virtual_users(email):
    user = find_user(email)
    now = datetime.utcnow()

    usage = user_virtual_usage.find_one({"email": email}) or {}
    last_reset = usage.get("last_reset", now - timedelta(days=7))
    used = usage.get("used_virtual_users", 0)

    # Determine if user is paid or trial
    is_paid = user.get("paid_ends_at") and user["paid_ends_at"] > now

    # Default limits
    weekly_limit = 1_000_000 if is_paid else 100

    # Custom per-user override
    if email == "viral@neeyatai.com":
        weekly_limit = 1000

    # Reset if 7 days passed
    if (now - last_reset).days >= 7:
        used = 0
        last_reset = now
        user_virtual_usage.update_one(
            {"email": email},
            {"$set": {"used_virtual_users": 0, "last_reset": last_reset}},
            upsert=True
        )

    remaining = max(0, weekly_limit - used)
    next_reset = last_reset + timedelta(days=7)

    return {"remaining": remaining, "is_paid": is_paid, "next_reset": next_reset}





def increment_virtual_user_usage(email, increment_by):
    from datetime import datetime
    usage = user_virtual_usage.find_one({"email": email}) or {}
    last_reset = usage.get("last_reset", datetime.utcnow())

    user_virtual_usage.update_one(
        {"email": email},
        {
            "$set": {"last_reset": last_reset},
            "$inc": {"used_virtual_users": increment_by}
        },
        upsert=True
    )




# Metrics helpers
def initialize_user_metrics(email):
    try:
        user_metrics.insert_one({
            "email": email,
            "total_test_plans_generated": 0,
            "total_tests_run": 0,
            "total_analysis_reports": 0
        })
    except Exception as e:
        print(f"Error initializing metrics for {email}: {e}")

def increment_user_metric(email, metric_key):
    if metric_key not in ["total_test_plans_generated", "total_tests_run", "total_analysis_reports"]:
        return

    try:
        user_metrics.update_one(
            {"email": email},
            {"$inc": {metric_key: 1}},
            upsert=True
        )

        # Update monthly metrics as well
        month = datetime.utcnow().strftime("%Y-%m")
        user_monthly_metrics.update_one(
            {"email": email, "month": month},
            {"$inc": {metric_key: 1}},
            upsert=True
        )
    except Exception as e:
        print(f"Error updating metric {metric_key} for {email}: {e}")

def get_user_metrics_with_comparison(email):
    current = user_metrics.find_one({"email": email}) or {}

    current_month = datetime.utcnow().strftime("%Y-%m")
    last_month = (datetime.utcnow().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")

    current_month_data = user_monthly_metrics.find_one({"email": email, "month": current_month}) or {}
    last_month_data = user_monthly_metrics.find_one({"email": email, "month": last_month}) or {}

    def pct_change(current_val, last_val):
        if last_val == 0:
            return None
        return round(((current_val - last_val) / last_val) * 100, 2)

    return {
        "total_test_plans_generated": current.get("total_test_plans_generated", 0),
        "total_test_plans_this_month": current_month_data.get("total_test_plans_generated", 0),
        "total_test_plans_last_month": last_month_data.get("total_test_plans_generated", 0),
        
        "total_tests_run": current.get("total_tests_run", 0),
        "total_tests_run_this_month": current_month_data.get("total_tests_run", 0),
        "total_tests_run_last_month": last_month_data.get("total_tests_run", 0),

        "total_analysis_reports": current.get("total_analysis_reports", 0),
        "total_analysis_reports_this_month": current_month_data.get("total_analysis_reports", 0),
        "total_analysis_reports_last_month": last_month_data.get("total_analysis_reports", 0),
    }


try:
    promo_codes.create_index("code", unique=True)
except Exception as e:
    print("Failed to create unique index on promo code:", e)
    

def get_valid_promo(code):
    return promo_codes.find_one({"code": code.upper(), "active": True})


# Ensure TTL index exists (run once or during app startup)
otp_codes.create_index("created_at", expireAfterSeconds=300)

def save_otp(email, hashed_otp):
    otp_entry = {
        "email": email,
        "otp": hashed_otp,
        "used": False,
        "created_at": datetime.utcnow()
    }
    otp_codes.insert_one(otp_entry)

def get_latest_otp(email):
    return otp_codes.find_one({"email": email, "used": False}, sort=[("created_at", -1)])

def mark_otp_used(email):
    otp_codes.update_many({"email": email, "used": False}, {"$set": {"used": True}})


def create_user(email, hashed_pw, name, mobile, organization, organization_type, country):
    user = {
        "email": email,
        "password": hashed_pw,
        "name": name,
        "mobile": mobile,
        "organization": organization,
        "organization_type": organization_type,
        "country": country,
        "created_at": datetime.utcnow(),
        "trial_ends_at": datetime.utcnow() + timedelta(days=15),
        "paid_ends_at": None,
        "is_verified": False,
        "deleted": False,
        "additional_emails": [],
    }
    print("User to insert:", user)
    try:
        result = users.insert_one(user)
        print("Inserted ID:", result.inserted_id)
        return user
    except Exception as e:
        print("Error inserting user:", str(e))
        return None


def find_user(email, include_deleted=False):
    query = {"email": email}
    if not include_deleted:
        query["$or"] = [{"deleted": {"$exists": False}}, {"deleted": False}]
    return users.find_one(query)

def mark_user_verified(email):
    return update_user(email, {"is_verified": True})

def update_user(email, update_dict):
    """
    Updates a user document in the database by email.

    :param email: The user's email address.
    :param update_dict: A dictionary of fields to update.
    :return: The result of the update operation.
    """
    try:
        result = users.update_one(
            {"email": email},
            {"$set": update_dict}
        )
        return result
    except Exception as e:
        print(f"Error updating user ({email}): {str(e)}")
        return None


api_tokens = db["api_tokens"]
api_tokens.create_index("token", unique=True)
api_tokens.create_index("user_id")


import secrets
from hashlib import sha256

def generate_secure_token():
    # 256-bit token
    return sha256(secrets.token_bytes(32)).hexdigest()

def save_api_token(user_id, email, expires_at):
    token = generate_secure_token()
    api_tokens.insert_one({
        "token": token,
        "email": email,
        "user_id": user_id,
        "created_at": datetime.utcnow(),
        "expires_at": expires_at
    })
    return token

