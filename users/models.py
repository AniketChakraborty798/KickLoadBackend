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

# Add at the top
otp_codes = db["otp_codes"]

promo_codes = db["promo_codes"]

# Add this after existing user, otp_codes, promo_codes setup

# Metrics collection (for tracking per-user stats)
user_metrics = db["user_metrics"]
user_monthly_metrics = db["user_monthly_metrics"]

# Ensure indexes
user_metrics.create_index("email", unique=True)
user_monthly_metrics.create_index([("email", ASCENDING), ("month", ASCENDING)], unique=True)

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
        "total_test_plans_last_month": last_month_data.get("total_test_plans_generated", 0),
        "total_test_plans_pct_change": pct_change(
            current.get("total_test_plans_generated", 0),
            last_month_data.get("total_test_plans_generated", 0)
        ),

        "total_tests_run": current.get("total_tests_run", 0),
        "total_tests_run_last_month": last_month_data.get("total_tests_run", 0),
        "total_tests_run_pct_change": pct_change(
            current.get("total_tests_run", 0),
            last_month_data.get("total_tests_run", 0)
        ),

        "total_analysis_reports": current.get("total_analysis_reports", 0),
        "total_analysis_reports_last_month": last_month_data.get("total_analysis_reports", 0),
        "total_analysis_reports_pct_change": pct_change(
            current.get("total_analysis_reports", 0),
            last_month_data.get("total_analysis_reports", 0)
        )
    }

try:
    promo_codes.create_index("code", unique=True)
except Exception as e:
    print("Failed to create unique index on promo code:", e)
    
def insert_ruslan_promo():
    promo_codes.update_one(
        {"code": "RUSLAN5"},
        {
            "$set": {
                "code": "RUSLAN5",
                "discount_percent": 5,
                "active": True
            }
        },
        upsert=True
    )

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
        "trial_ends_at": datetime.utcnow() + timedelta(days=5),
        "paid_ends_at": None,
        "is_verified": False,
        "deleted": False
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

def create_ayush_user():
    email = "ayushbora1001@gmail.com"
    raw_password = "90opl;./()P"  # Replace with actual password
    hashed_pw = hash_password(raw_password)

    # bcrypt returns bytes, convert to string if necessary
    if isinstance(hashed_pw, bytes):
        hashed_pw = hashed_pw.decode()

    user_data = create_user(
        email=email,
        hashed_pw=hashed_pw,
        name="Ayush",
        mobile="8178513819",
        organization="Neeyatai",
        organization_type="startup",
        country="India"
    )

    if user_data:
        # Set 1 year paid validity and mark verified
        one_year_from_now = datetime.utcnow() + timedelta(days=365)
        update_user(email, {
            "paid_ends_at": one_year_from_now,
            "is_verified": True
        })
        print(f"✅ User {email} created with 1-year validity and verified.")
    else:
        print("❌ Failed to create Ayush user.")

