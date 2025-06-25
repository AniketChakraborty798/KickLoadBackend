import os
import sys
import logging
from flask import Flask, request, jsonify
from flask_cors import CORS
from intelligent_test_analysis import analyze_jtl
from tasks.tasks import run_jmeter_test_async
from generate_test_plan import generate_jmeter_test_plan
from datetime import datetime
from flask_jwt_extended import jwt_required, get_jwt_identity
from users.auth import auth_bp
from email_utils import send_email
from users import init_jwt
from users.utils import s3, BUCKET_NAME, download_file_from_s3, upload_file_to_s3, generate_presigned_url
import tempfile
from users import limiter
from payments.routes import payments_bp
from email_utils import styled_email_template
from dotenv import load_dotenv
import time
from users.models import get_user_metrics_with_comparison ,increment_user_metric
import re

load_dotenv()

def get_user_prefix():
    return f"uploads/{get_jwt_identity()}/"

def sanitize_email_for_path(email: str) -> str:
    return email.replace("@", "_at_").replace(".", "_dot_")

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler('app.log'), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# ---------- Flask App ----------
app = Flask(__name__)
init_jwt(app)
limiter.init_app(app)

# Register Blueprints
app.register_blueprint(auth_bp)
app.register_blueprint(payments_bp, url_prefix="/payments")

app.config['REDIS_URL'] = os.getenv("REDIS_URL")


# Enable CORS (adjust domains before production)
CORS(app,
     supports_credentials=True,
     origins=[
         os.getenv("CORS_ORIGIN")])

@app.before_request
def handle_options():
    if request.method == 'OPTIONS':
        return '', 200

# ---------- Routes ----------
@app.route("/", methods=["GET"])
def index():
    return jsonify({"status": "success", "message": "Server is running."}), 200

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
        if file_type not in ["jmx", "jtl", "md"]:
            return jsonify({"error": "Invalid file type requested. Must be 'jmx', 'jtl', or 'md'."}), 400

        user_prefix = get_user_prefix()
        response = s3.list_objects_v2(Bucket=BUCKET_NAME, Prefix=user_prefix)

        if "Contents" not in response:
            return jsonify([])

        result = []

        for obj in response.get("Contents", []):
            key = obj["Key"]
            if not key.endswith(f".{file_type}"):
                continue

            filename = key.split("/")[-1]  # strip folder prefix
            # Expecting format like test_plan_21-06-2025_17-43-46.jmx
            match = re.search(r"_(\d{2}-\d{2}-\d{4}_\d{2}-\d{2}-\d{2})", filename)
            if not match:
                continue  # skip if pattern not found

            try:
                dt_str = match.group(1)
                dt_obj = datetime.strptime(dt_str, "%d-%m-%Y_%H-%M-%S")
            except ValueError:
                continue  # skip malformed datetime

            result.append({
                "filename": filename,
                "datetime": dt_obj.isoformat()
            })

        # Sort by extracted datetime descending
        result.sort(key=lambda x: x["datetime"], reverse=True)

        return jsonify(result)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/run-test/<test_filename>', methods=['POST'])
@jwt_required()
def run_test(test_filename):
    try:
        email = get_jwt_identity()
        if not test_filename.endswith(".jmx"):
            return jsonify({'status': 'error', 'message': 'Invalid test file format. Must be .jmx'}), 400

        user_prefix = get_user_prefix()

        # ✅ Send only metadata to Celery, let it download and isolate
        task = run_jmeter_test_async.delay(f"{user_prefix}{test_filename}")
        summary_output = task.get(timeout=60)


        result_file = test_filename.replace(".jmx", ".jtl")
        increment_user_metric(email, "total_tests_run")
        return jsonify({
            "status": "success",
            "message": "JMeter test executed.",
            "result_file": result_file,
            "summary_output": summary_output
        })

    except Exception as e:
        logger.error(f"Run test error: {str(e)}")
        return jsonify({'status': 'error', 'message': f'Failed to run test: {str(e)}'}), 500




@app.route("/analyzeJTL", methods=["POST"])
@limiter.limit("5/minute")
@jwt_required()
def analyze_jtl_api():
    try:
        email = get_jwt_identity()
        data = request.get_json()
        jtl_filename = data.get("filename")

        if not jtl_filename or not jtl_filename.endswith(".jtl"):
            return jsonify({"error": "Invalid or missing .jtl filename"}), 400

        # Step 1: Download .jtl file from S3 to temp location
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jtl") as temp_jtl_file:
            local_jtl_path = temp_jtl_file.name
        user_prefix = get_user_prefix()
        download_file_from_s3(f"{user_prefix}{jtl_filename}", local_jtl_path)

        # Step 2: Run analysis and generate .md file in temp dir
        with tempfile.TemporaryDirectory() as temp_analysis_dir:
            result = analyze_jtl(local_jtl_path, temp_analysis_dir)

            # Step 3: Upload analysis file to S3
            md_filename = result.get("filename")
            if md_filename:
                md_path = os.path.join(temp_analysis_dir, md_filename)
                upload_file_to_s3(md_path, f"{user_prefix}{md_filename}")

        # Step 4: Clean up
        os.remove(local_jtl_path)
        increment_user_metric(email, "total_analysis_reports")
        return jsonify(result)

    except Exception as e:
        return jsonify({"error": f"JTL analysis error: {str(e)}"}), 500

@app.route("/sendEmail", methods=["POST"])
@jwt_required()
def send_email_api():
    try:
        data = request.get_json()
        md_filename = data.get("filename")

        if not md_filename or not md_filename.endswith(".md"):
            return jsonify({"error": "A valid .md filename is required."}), 400

        current_user_email = get_jwt_identity()
        if not current_user_email:
            return jsonify({"error": "Unable to determine recipient email."}), 400

        # Download the .md file from S3
        with tempfile.NamedTemporaryFile(delete=False, suffix=".md") as temp_file:
            local_md_path = temp_file.name
        user_prefix = get_user_prefix()
        download_file_from_s3(f"{user_prefix}{md_filename}", local_md_path)

        # Styled HTML body
        body = styled_email_template(
        title="JTL Analysis Summary",
        message="""
            Hello,<br><br>
            Please find attached the summary of your recent JTL performance analysis.<br><br>
            If you have any questions or need support, feel free to contact our team.
        """
    )

        response = send_email(
            to=current_user_email,
            subject="JTL Analysis Summary",
            body=body,
            attachments=[(local_md_path, md_filename)],  # Pass (path, original filename)
            is_html=True
        )


        os.remove(local_md_path)
        return jsonify(response)

    except Exception as e:
        return jsonify({"error": f"Email error: {str(e)}"}), 500



@app.route("/generate-test-plan", methods=["POST"])
@limiter.limit("5/minute")
@jwt_required()
def generate_test_plan_api():
    try:
        email = get_jwt_identity()
        data = request.json
        prompt = data.get("prompt")

        if not prompt:
            return jsonify({"status": "error", "message": "Prompt is missing."}), 400

        user_email = get_jwt_identity()  # ✅ Grab email from JWT
        result, code = generate_jmeter_test_plan(prompt, user_email)
        increment_user_metric(email, "total_test_plans_generated")
        return jsonify(result), code

    except Exception as e:
        return jsonify({"status": "error", "message": f"Failed to generate test plan: {str(e)}"}), 500


@app.route('/download/<filename>', methods=['GET'])
@jwt_required()
def universal_download(filename):
    try:
        user_prefix = get_user_prefix()
        s3_key = f"{user_prefix}{filename}"
        url = generate_presigned_url(s3_key)
        
        if url:
            return jsonify({"status": "success", "download_url": url})
        else:
            return jsonify({"status": "error", "message": "Failed to generate download URL"}), 500

    except Exception as e:
        return jsonify({'status': 'error', 'message': f'Download error: {str(e)}'}), 500
