import os
import logging
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from intelligent_test_analysis import analyze_jtl_to_pdf
from tasks.tasks import run_jmeter_test_async
from generate_test_plan import generate_and_upload_jmx, is_valid_jmx, extract_user_count_from_jmx, read_and_validate_data_file, inject_csv_dataset_into_jmx, is_valid_jmeter_prompt, enforce_core_jmeter_defaults
from users.licence_utils import get_license_info
from datetime import datetime, timezone
from flask_jwt_extended import jwt_required, get_jwt_identity
from users.auth import auth_bp
from email_utils import send_email, styled_email_template
from users import init_jwt
from users.utils import s3, BUCKET_NAME, download_file_from_s3, upload_fileobj_to_s3, upload_file_to_s3, generate_presigned_url
import tempfile
from users import limiter
from payments.routes import payments_bp
from dotenv import load_dotenv
from users.models import get_user_metrics_with_comparison ,increment_user_metric, find_user, update_user, get_remaining_virtual_users, increment_virtual_user_usage, api_tokens, save_api_token
import re
import io
import traceback
from celery.result import AsyncResult
from jenkins.jenkins_routes import jenkins_bp
from jenkins.github_integration import github_bp
from auth.decorators import dual_auth_required
from tasks.celery import celery
import json
from jmeter_core import parse_jtl_summary
from utils.pdf_generator import generate_pdf_report
from asgutils.asg import scale_asg_for_vus, discover_asg_worker_ips



load_dotenv()

def get_user_prefix():
    if hasattr(request, "api_user"):
        email = request.api_user["email"]
    else:
        # fallback to JWT identity
        identity = get_jwt_identity()
        email = identity
    return f"uploads/{email}/"


def sanitize_email_for_path(email: str) -> str:
    return email.replace("@", "_at_").replace(".", "_dot_")

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('app.log'), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

cors_origin = os.getenv("CORS_ORIGIN", "https://kickload.neeyatai.com")
allowed_origins = [
    "https://neeyatai.com",
    "https://www.neeyatai.com",
    "https://kickload.neeyatai.com",
    "http://localhost:3000",
    "http://localhost:5173",
]


CORS(app, supports_credentials=True, origins=allowed_origins)

init_jwt(app)
limiter.init_app(app)

app.register_blueprint(auth_bp)
app.register_blueprint(payments_bp, url_prefix="/payments")
app.register_blueprint(jenkins_bp, url_prefix="/jenkins")
app.register_blueprint(github_bp, url_prefix="/github")

# ---------- Flask App ----------

app.config['REDIS_URL'] = os.getenv("REDIS_URL")



# ---------- Routes ----------
@app.route("/", methods=["GET"])
def index():
    return jsonify({"status": "success", "message": "Server is running."}), 200

@app.route("/request-demo", methods=["POST"])
def request_demo():
    try:
        data = request.get_json() or {}
        name = data.get("name", "").strip()
        email = data.get("email", "").strip()
        mobile = data.get("mobile", "").strip()

        if not name or not email or "@" not in email:
            return jsonify({"success": False, "message": "Invalid name or email"}), 400

        ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "viral@neeyatai.com")

        # Create nicely styled email
        message_html = f"""
        <p><strong>Name:</strong> {name}</p>
        <p><strong>Email:</strong> {email}</p>
        <p><strong>Mobile:</strong> {mobile or 'N/A'}</p>
        """
        email_body = styled_email_template(
            title="New Demo Request",
            message=message_html
        )

        # Send email
        result = send_email(
            to=ADMIN_EMAIL,
            subject="New Demo Request - KickLoad",
            body=email_body,
            is_html=True
        )

        if "error" in result:
            return jsonify({"success": False, "message": "Failed to send email"}), 500

        return jsonify({"success": True, "message": "Request sent successfully!"}), 200

    except Exception as e:
        current_app.logger.error(f"Request demo error: {e}")
        return jsonify({"success": False, "message": "Server error"}), 500


@app.route("/api-token", methods=["GET"])
@jwt_required()
def get_api_token():
    email = get_jwt_identity()
    user_doc = find_user(email)

    if not user_doc:
        return jsonify({"error": "User not found"}), 404

    token_doc = api_tokens.find_one({"user_id": user_doc["_id"]})
    if not token_doc:
        return jsonify({"message": "No token found."}), 404

    return jsonify({
        "api_token": token_doc["token"],  # or mask part if needed
        "valid_until": token_doc["expires_at"],
        "created_at": token_doc["created_at"]
    }), 200


@app.route("/generate-api-token", methods=["POST"])
@jwt_required()
def generate_api_token():
    email = get_jwt_identity()
    user_doc = find_user(email)

    if not user_doc:
        return jsonify({"error": "User not found"}), 404

    now = datetime.utcnow()
    end_time = user_doc.get("paid_ends_at") or user_doc.get("trial_ends_at")

    if not end_time or end_time < now:
        return jsonify({"error": "Your access period has expired."}), 403

    # 🔒 Enforce one token per user
    existing_token = api_tokens.find_one({"user_id": user_doc["_id"]})
    if existing_token:
        return jsonify({
            "error": "API token already exists. You must revoke it before generating a new one.",
            "token": existing_token["token"],
            "valid_until": existing_token["expires_at"]
        }), 409  # Conflict

    # ✅ Generate new token
    token = save_api_token(user_doc["_id"], user_doc["email"], end_time)
    return jsonify({"api_token": token, "valid_until": end_time}), 201


@app.route("/revoke-api-token", methods=["POST"])
@jwt_required()
def revoke_api_token():
    email = get_jwt_identity()
    user_doc = find_user(email)

    if not user_doc:
        return jsonify({"error": "User not found"}), 404

    result = api_tokens.delete_one({"user_id": user_doc["_id"]})
    if result.deleted_count == 0:
        return jsonify({"message": "No token found to revoke."}), 404

    return jsonify({"message": "API token revoked successfully."}), 200







@app.route("/cookie-debug")
def cookie_debug():
    return jsonify({
        "origin": request.headers.get("Origin"),
        "remote_ip": request.headers.get("X-Forwarded-For", request.remote_addr),
        "access_token_cookie": request.cookies.get("access_token_cookie"),
        "refresh_token_cookie": request.cookies.get("refresh_token_cookie"),
        "user_agent": request.headers.get("User-Agent")
    })



@app.route("/rename-file", methods=["POST"])
@jwt_required()
def rename_file():
    try:
        data = request.get_json()
        old_name = data.get("old_name")
        new_name = data.get("new_name")

        if not old_name or not new_name:
            return jsonify({"error": "Both old and new filenames are required."}), 400

        user_prefix = get_user_prefix()
        old_key = f"{user_prefix}{old_name}"
        new_key = f"{user_prefix}{new_name}"

        # ❌ Check if new file already exists
        try:
            s3.head_object(Bucket=BUCKET_NAME, Key=new_key)
            return jsonify({"error": "A file with the new name already exists."}), 400
        except s3.exceptions.ClientError as e:
            if e.response['Error']['Code'] != "404":
                raise  # re-raise if it's not a 'not found' error

        # ✅ Copy and delete
        s3.copy_object(Bucket=BUCKET_NAME, CopySource={"Bucket": BUCKET_NAME, "Key": old_key}, Key=new_key)
        s3.delete_object(Bucket=BUCKET_NAME, Key=old_key)

        return jsonify({"status": "success", "message": f"File renamed from {old_name} to {new_name}"}), 200

    except Exception as e:
        return jsonify({"error": f"Rename failed: {str(e)}"}), 500


@app.route("/delete-file", methods=["POST"])
@jwt_required()
def delete_file():
    try:
        data = request.get_json()
        filename = data.get("filename", "").strip()

        if not filename:
            return jsonify({"error": "Filename is required."}), 400

        s3_key = f"{get_user_prefix()}{filename}"

        # Optional: check if file exists first
        try:
            s3.head_object(Bucket=BUCKET_NAME, Key=s3_key)
        except s3.exceptions.ClientError as e:
            if e.response["Error"]["Code"] == "404":
                return jsonify({"error": "File not found."}), 404
            else:
                raise  # propagate other errors

        # Delete file permanently
        s3.delete_object(Bucket=BUCKET_NAME, Key=s3_key)

        return jsonify({"status": "success", "message": f"{filename} deleted."}), 200

    except Exception as e:
        return jsonify({"error": f"Delete failed: {str(e)}"}), 500




@app.route("/additional-emails", methods=["GET"])
@jwt_required()
def get_additional_emails():
    email = get_jwt_identity()
    user = find_user(email)
    return jsonify(user.get("additional_emails", [])), 200

@app.route("/add-email", methods=["POST"])
@jwt_required()
def add_additional_email():
    data = request.get_json()
    new_email = data.get("email", "").strip().lower()

    if not new_email or "@" not in new_email:
        return jsonify({"error": "Invalid email."}), 400

    email = get_jwt_identity()
    user = find_user(email)

    if not user:
        return jsonify({"error": "User not found."}), 404

    if new_email in user.get("additional_emails", []):
        return jsonify({"message": "Email already added."}), 200

    update_user(email, {"additional_emails": list(set(user.get("additional_emails", []) + [new_email]))})

    return jsonify({"message": "Email added successfully."}), 200


@app.route("/remove-email", methods=["POST"])
@jwt_required()
def remove_additional_email():
    data = request.get_json()
    remove_email = data.get("email", "").strip().lower()

    if not remove_email:
        return jsonify({"error": "Invalid email."}), 400

    email = get_jwt_identity()
    user = find_user(email)

    if not user:
        return jsonify({"error": "User not found."}), 404

    updated_list = [e for e in user.get("additional_emails", []) if e != remove_email]
    update_user(email, {"additional_emails": updated_list})

    return jsonify({"message": "Email removed successfully."}), 200




@app.route("/user-metrics", methods=["GET"])
@jwt_required()
def get_metrics():
    email = get_jwt_identity()
    data = get_user_metrics_with_comparison(email)
    return jsonify(data), 200

    
@app.route("/list-files", methods=["GET"])
@jwt_required()
def list_files():
    try:
        file_type = request.args.get("type", "").lower()
        filter_prefix = request.args.get("filter_prefix", "")  # optional

        if file_type not in ["jmx", "jtl", "pdf"]:
            return jsonify({"error": "Invalid file type requested. Must be 'jmx', 'jtl', or 'pdf'."}), 400

        user_prefix = get_user_prefix()
        response = s3.list_objects_v2(Bucket=BUCKET_NAME, Prefix=user_prefix)

        if "Contents" not in response:
            return jsonify([])

        result = []

        for obj in response.get("Contents", []):
            key = obj["Key"]
            filename = key.split("/")[-1]

            # Filter by extension
            if not filename.endswith(f".{file_type}"):
                continue

            # Optional prefix filter (e.g., "test_plan_")
            if filter_prefix and not filename.startswith(filter_prefix):
                continue

            last_modified = obj["LastModified"]  # This is a datetime object (timezone-aware)

            result.append({
                "filename": filename,
                "datetime": last_modified.isoformat()
            })

        # Sort by S3's last modified time (newest first)
        result.sort(key=lambda x: x["datetime"], reverse=True)

        return jsonify(result)

    except Exception as e:
        return jsonify({"error": str(e)}), 500



@app.route("/extract-params", methods=["GET"])
@jwt_required()
def extract_params():
    try:
        filename = request.args.get("file")
        if not filename or not filename.endswith(".jmx"):
            return jsonify({"status": "error", "message": "Missing or invalid filename"}), 400

        user_prefix = get_user_prefix()
        s3_key = f"{user_prefix}{filename}"
        local_path = f"/tmp/{filename}"

        download_file_from_s3(s3_key, local_path)

        from extract_params_from_jmx import extract_editable_params
        param_data = extract_editable_params(local_path)

        return jsonify({
            "status": "success",
            "filename": filename,
            "params": param_data
        })

    except Exception as e:
        logger.error(f"Extract params error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

import redis
from flask import Response, stream_with_context

redis_client = redis.StrictRedis(host="redis", port=6379, password=os.getenv("REDIS_PASSWORD"), decode_responses=True)

@app.route('/stream-logs/<channel>')
def stream_logs(channel):
    def event_stream():
        pubsub = redis_client.pubsub()
        pubsub.subscribe(channel)

        for message in pubsub.listen():
            if message['type'] == 'message':
                yield f"data: {message['data']}\n\n"

    return Response(stream_with_context(event_stream()), mimetype="text/event-stream")





@app.route("/task-status/<task_id>", methods=["GET"])
@dual_auth_required
def get_task_status(task_id):
    task = AsyncResult(task_id)
    logger.info(f"Task {task_id} status checked: {task.state}")

    if task.state in ["PENDING", "STARTED"]:
        return jsonify({"status": "running"})

    elif task.state == "SUCCESS":
        return jsonify({
            "status": "success",
            "result_file": task.result.get("filename"),
            "pdf_file": task.result.get("pdf_filename"),
            "summary_output": task.result.get("summary")
        })

    elif task.state == "FAILURE":
        return jsonify({
            "status": "error",
            "message": "Test failed. Please check your test plan or try again."
        })

    return jsonify({"status": "unknown"})



@app.route("/stop-test/<task_id>", methods=["POST"])
@dual_auth_required
def stop_test(task_id):
    import os, signal, time, subprocess, json

    try:
        pid = redis_client.get(f"jmeter_pid:{task_id}")
        vuinfo = redis_client.get(f"jmeter_vuinfo:{task_id}")
        meta = redis_client.get(f"jmeter_meta:{task_id}")
        mode = redis_client.get(f"jmeter_mode:{task_id}")  # Should be "local" or "distributed", save at test start

        # Distributed mode: paid user, ASG/remote slaves
        if mode == "distributed":
            JMETER_BIN = "/opt/apache-jmeter-5.6.3/bin"
            shutdown_cmd = [os.path.join(JMETER_BIN, "shutdown.sh")]
            try:
                result = subprocess.run(shutdown_cmd, check=True, capture_output=True)
                logger.info(f"Distributed JMeter shutdown.sh executed for task {task_id}, stdout: {result.stdout}, stderr: {result.stderr}")
            except Exception as e:
                logger.error(f"Failed to execute shutdown.sh for distributed test {task_id}: {str(e)}")
            # Optional: you may want to scale ASG down to 0 here for safety, or wait for auto scale in Celery task

        # Local mode: trial user, only single EC2 instance
        else:
            if not pid:
                logger.error(f"No JMeter PID found for task {task_id}")
                return jsonify({"status": "error", "message": "No running JMeter process found for this test."}), 404
            pid = int(pid)
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
                logger.info(f"Sent SIGTERM to JMeter process group for task {task_id}, PID {pid}")
                time.sleep(5)
                os.killpg(os.getpgid(pid), 0)
            except ProcessLookupError:
                logger.info(f"JMeter process group for task {task_id} terminated gracefully.")
            except Exception as e:
                logger.warning(f"Graceful shutdown failed or timed out, sending SIGKILL: {e}")
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                    logger.info(f"Sent SIGKILL to JMeter process group for task {task_id}, PID {pid}")
                except Exception as ex:
                    logger.error(f"Error killing JMeter PID {pid} with SIGKILL: {ex}")

        # Revoke Celery task with SIGTERM, applies for both modes
        try:
            celery.control.revoke(task_id, terminate=True, signal="SIGTERM")
        except Exception as e:
            logger.error(f"Failed to revoke Celery task for {task_id}: {str(e)}")


        # Clean Redis keys
        for key in [f"jmeter_pid:{task_id}", f"jmeter_vuinfo:{task_id}", f"jmeter_meta:{task_id}", f"jmeter_mode:{task_id}"]:
            try:
                redis_client.delete(key)
            except Exception as cleanup_exc:
                logger.warning(f"Error cleaning Redis key {key}: {cleanup_exc}")

        return jsonify({"status": "stopped", "message": "Test stopped successfully."})

    except Exception as e:
        logger.error(f"Failed to stop test {task_id}: {e}")
        return jsonify({"status": "error", "message": f"Failed to stop test: {str(e)}"}), 500



@app.route("/remaining-virtual-users", methods=["GET"])
@jwt_required()
def get_remaining_users():
    email = get_jwt_identity()
    user = find_user(email)

    if not user:
        return jsonify({"error": "User not found"}), 404

    now = datetime.utcnow()
    is_paid = user.get("paid_ends_at") and user["paid_ends_at"] > now
    data = get_remaining_virtual_users(email)

    return jsonify({
        "remaining_virtual_users": data["remaining"],
        "is_paid": is_paid,
        "next_reset": data["next_reset"].isoformat()
    })




@app.route('/run-test/<test_filename>', methods=['POST'])
@dual_auth_required
def run_test(test_filename):
    try:
        email = request.api_user["email"]

        if not test_filename.endswith(".jmx"):
            logger.warning(f"Invalid file extension for user {email}: {test_filename}")
            return jsonify({'status': 'error', 'message': 'Invalid test file format. Must be .jmx'}), 400

        user_prefix = get_user_prefix()
        overrides = request.get_json(silent=True) or {}

        user = find_user(email)
        license_info = get_license_info(user)
        license_type = license_info["license"]

        if not isinstance(overrides, dict):
            logger.warning(f"Invalid overrides payload for user {email}: {overrides}")
            overrides = {}

        num_threads = int(overrides.get("num_threads", 0))
        remaining_info = get_remaining_virtual_users(email)
        remaining = remaining_info["remaining"]

        # ===============================
        # 🚦 Validation for thread lifetime and duration
        # ===============================
        specify_thread_lifetime = overrides.get("specify_thread_lifetime", False)
        duration = overrides.get("duration")
        startup_delay = overrides.get("startup_delay")
        loop_count = overrides.get("loop_count")

        # Ensure they are clean integers where needed
        try:
            duration_val = int(duration) if duration not in [None, ""] else None
            loop_count_val = int(loop_count) if loop_count not in [None, ""] else None
        except ValueError:
            logger.warning(f"Non-numeric loop count or duration for user {email}: loop_count={loop_count}, duration={duration}")
            return jsonify({"status": "error", "message": "Loop count / duration must be a number"}), 400

        # Allow duration/startup_delay even if specify_thread_lifetime is False, but warn if startup_delay used without specify
        if not specify_thread_lifetime and startup_delay:
            logger.warning(f"Startup delay provided without specify_thread_lifetime for user {email}: startup_delay={startup_delay}")

        # Rule 2: If specify_thread_lifetime is enabled, duration must be set and ≤ 60
        if specify_thread_lifetime:
            if not duration_val:
                logger.warning(f"Duration missing when specify_thread_lifetime enabled for user {email}")
                return jsonify({
                    "status": "error",
                    "message": "Duration is required when 'Specify Thread Lifetime' is enabled."
                }), 400
            if duration_val > 60:
                logger.warning(f"Duration {duration_val} exceeds max 60 for user {email}")
                return jsonify({
                    "status": "error",
                    "message": "Duration cannot exceed 60 seconds."
                }), 400

        # Trial & Paid: check against weekly quota
        limit = 100 if license_type == "trial" else 1_000_000

        if num_threads > remaining:
            logger.warning(f"User {email} exceeded virtual user limit: requested {num_threads}, remaining {remaining}")
            return jsonify({
                "status": "error",
                "message": f"You have {remaining:,} virtual users remaining this week. Please wait for reset or upgrade."
            }), 403

        # All checks passed: launch async test
        if license_type == "trial":
            task = run_jmeter_test_async.delay(f"{user_prefix}{test_filename}", overrides, user_email=email)
        else:
            # PAID users: distributed run on ASG
            # 1. Scale out ASG workers
            scale_asg_for_vus(num_threads)
            # 2. Wait for workers to be ready
            worker_ips = discover_asg_worker_ips()
            # 3. Run distributed JMeter
            task = run_jmeter_distributed_test_async.delay(
                f"{user_prefix}{test_filename}",
                overrides,
                worker_ips,
                user_email=email
            )

        increment_user_metric(email, "total_tests_run")

        # Update usage count for both trial and paid users
        increment_virtual_user_usage(email, num_threads)

        return jsonify({
            "status": "started",
            "task_id": task.id,
            "message": "Test started, you can check status using the task ID."
        })

    except Exception as e:
        logger.error(f"Run test error for user {email}: {str(e)}")
        return jsonify({'status': 'error', 'message': f'Failed to run test: {str(e)}'}), 500




@app.route("/analyzeJTL", methods=["POST"])
@limiter.limit("5/minute")
@dual_auth_required
def analyze_jtl_api():
    try:
        email = request.api_user["email"]

        data = request.get_json()
        jtl_filename = data.get("filename")

        if not jtl_filename or not jtl_filename.endswith(".jtl"):
            return jsonify({"error": "Invalid or missing .jtl filename"}), 400

        with tempfile.NamedTemporaryFile(delete=False, suffix=".jtl") as temp_jtl_file:
            local_jtl_path = temp_jtl_file.name

        user_prefix = get_user_prefix()
        download_file_from_s3(f"{user_prefix}{jtl_filename}", local_jtl_path)

        with tempfile.TemporaryDirectory() as temp_analysis_dir:
            result = analyze_jtl_to_pdf(local_jtl_path, temp_analysis_dir)

            pdf_filename = result.get("filename")
            if pdf_filename:
                pdf_path = os.path.join(temp_analysis_dir, pdf_filename)
                upload_file_to_s3(pdf_path, f"{user_prefix}{pdf_filename}")

        os.remove(local_jtl_path)
        increment_user_metric(email, "total_analysis_reports")
        if request.token_type == "api":
            result.pop("html_report", None)
            

        return jsonify(result)

    except Exception as e:
        return jsonify({"error": f"JTL analysis error: {str(e)}"}), 500


@app.route("/sendEmail", methods=["POST"])
@jwt_required()
def send_email_api():
    try:
        data = request.get_json()
        pdf_filename = data.get("filename")
        html_body = data.get("html_report")

        if not pdf_filename or not pdf_filename.endswith(".pdf"):
            return jsonify({"error": "A valid .pdf filename is required."}), 400
        if not html_body:
            return jsonify({"error": "Missing HTML report content"}), 400

        current_user_email = get_jwt_identity()
        if not current_user_email:
            return jsonify({"error": "Unable to determine recipient email."}), 400
        user = find_user(current_user_email)
        target_emails = [current_user_email] + user.get("additional_emails", [])

        response = send_email(
            to=target_emails,
            subject="KickLoad - JTL Analysis Report",
            body=html_body,
            is_html=True
        )

        if response.get("error"):
            return jsonify({"success": False, "error": response["error"]}), 500
        else:
            return jsonify({"success": True, "message": response.get("message", "Email sent.")}), 200

    except Exception as e:
        return jsonify({"error": f"Email error: {str(e)}"}), 500



@app.route("/sendEmailWithPDF", methods=["POST"])
@jwt_required()
def send_email_with_pdf():
    tmp_path = None
    try:
        data = request.get_json()
        filename = data.get("filename")

        if not filename or not filename.endswith(".pdf"):
            return jsonify({"error": "A valid PDF filename is required."}), 400

        current_user_email = get_jwt_identity()
        if not current_user_email:
            return jsonify({"error": "Unable to determine recipient email."}), 400

        user_prefix = get_user_prefix()
        s3_key = f"{user_prefix}{filename}"

        # Download PDF to temp file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_file:
            tmp_path = tmp_file.name
            if not download_file_from_s3(s3_key, tmp_path):
                return jsonify({"error": f"Failed to download {filename} from S3"}), 500
        user = find_user(current_user_email)
        target_emails = [current_user_email] + user.get("additional_emails", [])
        # Styled HTML email
        email_body = styled_email_template(
            title="KickLoad - Test Report",
            message="Please find your performance test report attached as a PDF."
        )

        # Send email with attachment
        response = send_email(
            to=target_emails,
            subject="KickLoad - Test Report",
            body=email_body,
            is_html=True,
            attachments=[(tmp_path, filename)]
        )

        if response.get("error"):
            return jsonify({"success": False, "error": response["error"]}), 500

        return jsonify({"success": True, "message": "Email sent successfully."}), 200

    except Exception as e:
        return jsonify({"error": f"Email send failed: {str(e)}"}), 500

    finally:
        # Always clean up temp file
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception as cleanup_err:
                app.logger.warning(f"Failed to delete temp file: {tmp_path} - {cleanup_err}")



@app.route("/generate-test-plan", methods=["POST"])
@limiter.limit("5/minute")
@dual_auth_required
def unified_generate_test_plan():
    try:
        email = request.api_user["email"]
        license_info = get_license_info(find_user(email))
        license_type = license_info["license"]

        file = request.files.get("file")  # JMX file
        data_file = request.files.get("data")  # CSV/XLSX file
        prompt = ""
        if request.is_json:
            prompt = request.json.get("prompt", "").strip()
        else:
            prompt = request.form.get("prompt", "").strip()

        # Always define once
        uploaded_xml = ""
        if file:
            uploaded_xml = file.read().decode("utf-8").strip()  # only read ONCE

        # ❌ CSV only is not allowed
        if data_file and not file and not prompt:
            return jsonify({"status": "error", "message": "Data file alone is not supported. Please include a prompt or JMX file."}), 400

        # ✅ Read CSV/XLSX if present
        csv_columns = []
        data_filename = None
        if data_file:
            df, original_filename, csv_columns = read_and_validate_data_file(data_file)

            # Check if uploaded file is Excel and convert to CSV
            if original_filename.lower().endswith('.xlsx'):
             
                csv_buffer = io.StringIO()
                df.to_csv(csv_buffer, index=False)
                csv_buffer.seek(0)

                csv_filename = original_filename.rsplit('.', 1)[0] + '.csv'
                key = f"uploads/{email}/{csv_filename}"
                upload_fileobj_to_s3(io.BytesIO(csv_buffer.getvalue().encode('utf-8')), key)
                data_filename = csv_filename  # Use CSV filename for injection
            else:
                # For CSV, upload as-is
                key = f"uploads/{email}/{original_filename}"
                data_file.stream.seek(0)
                upload_fileobj_to_s3(data_file.stream, key)
                data_filename = original_filename



        # ✅ Prompt only
        if prompt and not file:
            is_valid, error_msg = is_valid_jmeter_prompt(prompt, uploaded_xml, csv_columns)
            if not is_valid:
                return jsonify({"status": "error", "message": error_msg}), 400

            result, code = generate_and_upload_jmx(
                prompt=prompt,
                email=email,
                license_type=license_type,
                data_columns=csv_columns,
                data_filename=data_filename
            )
            if result["status"] == "success":
                increment_user_metric(email, "total_test_plans_generated")
            return jsonify(result), code

        # ✅ JMX only
        elif file and not prompt:
            if not uploaded_xml:
                return jsonify({"status": "error", "message": "Uploaded JMX file is empty."}), 400

            valid, reason = is_valid_jmx(uploaded_xml)
            if not valid:
                logger.warning(f"Uploaded JMX invalid: {reason}")
                result, code = generate_and_upload_jmx(
                    prompt="",  
                    email=email,
                    original_filename=file.filename,
                    uploaded_xml=uploaded_xml,
                    license_type=license_type,
                    data_columns=csv_columns,
                    data_filename=data_filename,
                    first_fail_reason=reason  # NEW
                )
                if result["status"] == "success":
                    increment_user_metric(email, "total_test_plans_generated")
                return jsonify(result), code


            # If valid, enforce defaults before saving
            uploaded_xml = enforce_core_jmeter_defaults(uploaded_xml)
            
            user_count = extract_user_count_from_jmx(uploaded_xml)
            if (license_type == "trial" and user_count > 100) or (license_type == "paid" and user_count > 1_000_000):
                return jsonify({"status": "error", "message": "User limit exceeded for your license."}), 403

            if csv_columns and data_filename:
                try:
                    uploaded_xml = inject_csv_dataset_into_jmx(uploaded_xml, data_filename, csv_columns)
                    valid, reason = is_valid_jmx(uploaded_xml)
                    if not valid:
                        result, code = generate_and_upload_jmx(
                            prompt="",
                            email=email,
                            original_filename=file.filename,
                            uploaded_xml=uploaded_xml,
                            license_type=license_type,
                            data_columns=csv_columns,
                            data_filename=data_filename,
                            first_fail_reason=reason
                        )

                        if result["status"] == "success":
                            increment_user_metric(email, "total_test_plans_generated")
                        return jsonify(result), code
                except Exception as e:
                    return jsonify({"status": "error", "message": f"CSV injection failed: {str(e)}"}), 400

            original_filename = file.filename
            timestamp = datetime.now(timezone.utc).strftime("%d-%m-%Y_%H-%M-%S")
            base = original_filename.rsplit(".", 1)[0]
            jmx_filename = f"{base}_{timestamp}.jmx"
            s3_key = f"uploads/{email}/{jmx_filename}"
            upload_fileobj_to_s3(io.BytesIO(uploaded_xml.encode("utf-8")), s3_key)


            increment_user_metric(email, "total_test_plans_generated")
            return jsonify({"status": "success", "message": "Uploaded JMX file validated and saved.", "jmx_filename": jmx_filename}), 200

        # ✅ Prompt + JMX
        elif file and prompt:
            if not uploaded_xml:
                return jsonify({"status": "error", "message": "Uploaded JMX file is empty."}), 400

            is_valid, error_msg = is_valid_jmeter_prompt(prompt, uploaded_xml, csv_columns)
            if not is_valid:
                return jsonify({"status": "error", "message": error_msg}), 400

            result, code = generate_and_upload_jmx(
                prompt=prompt,
                email=email,
                original_filename=file.filename,
                uploaded_xml=uploaded_xml,
                license_type=license_type,
                data_columns=csv_columns,
                data_filename=data_filename
            )
            if result["status"] == "success":
                increment_user_metric(email, "total_test_plans_generated")
            return jsonify(result), code

        return jsonify({"status": "error", "message": "Missing input: please provide a prompt, a JMX file, or both."}), 400

    except Exception as e:
        print("❌ Unified test plan generation error:", traceback.format_exc())
        return jsonify({"status": "error", "message": f"Internal server error: {str(e)}"}), 500


@app.route("/compare-jtls", methods=["POST"])
@dual_auth_required
def compare_jtls_api():
    try:
        data = request.get_json()
        filenames = data.get("filenames", [])

        # ✅ Must provide at least 2 files
        if not isinstance(filenames, list) or len(filenames) < 2:
            return jsonify({"error": "Provide at least 2 .jtl filenames for comparison"}), 400

        # ✅ Ensure all files are .jtl
        invalid_files = [f for f in filenames if not f.endswith(".jtl")]
        if invalid_files:
            return jsonify({"error": f"Invalid files: {', '.join(invalid_files)}. Only .jtl files allowed."}), 400

        email = request.api_user["email"]

        from tasks.tasks import compare_jtls_with_gemini_async

        # 🔁 Trigger async task and wait for result
        task = compare_jtls_with_gemini_async.delay(email, filenames)
        result = task.get(timeout=300)  # Wait up to 5 minutes

        if not result or result.get("status") != "success":
            return jsonify({"error": "Comparison failed"}), 500
        if request.token_type == "api":
            result.pop("html_report", None)
            result.pop("summary", None)

        return jsonify(result), 200

    except Exception as e:
        return jsonify({"error": f"Comparison failed: {str(e)}"}), 500




@app.route('/download/<filename>', methods=['GET'])
@dual_auth_required
def universal_download(filename):
    try:
        user_prefix = get_user_prefix()
        s3_key = f"{user_prefix}{filename}"
        
        # Decide based on query param
        mode = request.args.get("mode", "attachment")  # default is download
        content_disposition = "inline" if mode == "inline" else "attachment"

        url = generate_presigned_url(s3_key, content_disposition)

        if url:
            return jsonify({"status": "success", "download_url": url})
        else:
            return jsonify({"status": "error", "message": "Failed to generate download URL"}), 500

    except Exception as e:
        return jsonify({'status': 'error', 'message': f'Download error: {str(e)}'}), 500



@app.route("/cra-line", methods=["POST"])
@limiter.limit("2/minute")
@dual_auth_required
def unified_pipeline_test():
    try:
        email = request.api_user["email"]
        user_prefix = get_user_prefix()
        license_info = get_license_info(find_user(email))
        license_type = license_info["license"]

        # Extract inputs
        file = request.files.get("file")
        data_file = request.files.get("data")
        prompt = request.json.get("prompt", "").strip() if request.is_json else request.form.get("prompt", "").strip()

        # Try getting JSON body, fallback to form-data for individual override keys
        overrides = request.get_json(silent=True)
        if not overrides:
            overrides = {
                "num_threads": request.form.get("num_threads"),
                "loop_count": request.form.get("loop_count"),
                "ramp_time": request.form.get("ramp_time")
            }

        # 👇 Step 0: Save context-local auth info
        original_api_user = request.api_user
        original_token_type = request.token_type

        # ✅ Step 1: Call generate-test-plan logic directly
        with app.test_request_context(
            path="/generate-test-plan",
            method="POST",
            data=request.form,
            json=request.get_json(silent=True),
            headers=dict(request.headers),
            environ_base=request.environ
        ):
            # ✅ Inject manually into new request context
            request.api_user = original_api_user
            request.token_type = original_token_type

            # ⚠️ Call the function directly (not the route) with patched context
            response = unified_generate_test_plan()
            if isinstance(response, tuple):
                response, status = response
            else:
                status = 200

            if status != 200:
                return response, status

            gen_result = response.get_json()

            if gen_result.get("status") != "success":
                return jsonify({"error": "Test plan generation failed", "details": gen_result}), 400

        jmx_filename = gen_result.get("jmx_filename")
        if not jmx_filename:
            return jsonify({"error": "No JMX filename returned"}), 400

        # ✅ Step 2: Run test (sync)
        task = run_jmeter_test_async.delay(f"{user_prefix}{jmx_filename}", overrides)
        task_id = task.id
        increment_virtual_user_usage(email, int(overrides.get("num_threads", 0)))
        increment_user_metric(email, "total_tests_run")

        # ✅ Step 3: Poll result (sync wait)
        import time
        from celery.result import AsyncResult

        timeout = 300  # seconds
        interval = 5   # polling every 5 seconds
        waited = 0

        while waited < timeout:
            result = AsyncResult(task_id)
            if result.ready():
                if result.successful():
                    jtl_filename = result.result.get("filename")
                    jtl_pdf_filename=result.result.get("pdf_filename")
                    break
                else:
                    return jsonify({"error": "Test run failed", "details": str(result.result)}), 500
            time.sleep(interval)
            waited += interval
        else:
            return jsonify({"error": "Test execution timeout"}), 504

        if not jtl_filename:
            return jsonify({"error": "No JTL file produced"}), 500

        # ✅ Step 4: Analyze JTL
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jtl") as temp_jtl_file:
            local_jtl_path = temp_jtl_file.name
        download_file_from_s3(f"{user_prefix}{jtl_filename}", local_jtl_path)

        with tempfile.TemporaryDirectory() as temp_analysis_dir:
            analysis_result = analyze_jtl_to_pdf(local_jtl_path, temp_analysis_dir)
            pdf_filename = analysis_result.get("filename")
            if pdf_filename:
                pdf_path = os.path.join(temp_analysis_dir, pdf_filename)
                upload_file_to_s3(pdf_path, f"{user_prefix}{pdf_filename}")
        os.remove(local_jtl_path)
        increment_user_metric(email, "total_analysis_reports")

        return jsonify({
            "status": "success",
            "message": "Full test pipeline completed",
            "jmx_filename": jmx_filename,
            "jtl_filename": jtl_filename,
            "jtl_pdf_filename":jtl_pdf_filename,
            "analysis_pdf_filename": pdf_filename
        })

    except Exception as e:
        logger.error(f"Pipeline error: {str(e)}")
        return jsonify({"error": f"Pipeline failed: {str(e)}"}), 500



