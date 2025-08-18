from flask import Blueprint, request, jsonify
from datetime import datetime, timezone
import tempfile, os, traceback

from users.models import find_user, get_remaining_virtual_users, increment_virtual_user_usage, increment_user_metric
from users.utils import upload_file_to_s3
from email_utils import send_email, styled_email_template

jenkins_bp = Blueprint("jenkins", __name__)


from flask_jwt_extended import jwt_required, get_jwt_identity
import requests

import logging

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)  # or INFO in production

def trigger_jenkins_from_backend(repo_url, branch, user_id, jmx_folder, jmx_files):
    import os
    import requests
    import logging

    logger = logging.getLogger("jenkins")
    jenkins_base = os.getenv("JENKINS_BASE_URL", "https://jenkins.neeyatai.com")
    crumb_url = f"{jenkins_base}/crumbIssuer/api/json"
    jenkins_url = f"{jenkins_base}/job/NeeyatAI-JMeter-Pipeline/buildWithParameters"
    jenkins_user = os.getenv("JENKINS_USER")
    jenkins_token = os.getenv("JENKINS_API_TOKEN")

    try:
        crumb_resp = requests.get(crumb_url, auth=(jenkins_user, jenkins_token), timeout=5)
        crumb_resp.raise_for_status()
        crumb_data = crumb_resp.json()
    except Exception as e:
        logger.exception("[Jenkins] Failed to get crumb")
        raise

    headers = {
        crumb_data["crumbRequestField"]: crumb_data["crumb"],
        "Content-Type": "application/x-www-form-urlencoded",
    }

    params = {
        "REPO_URL": repo_url,
        "BRANCH": branch,
        "USER_ID": user_id,
        "JMX_FOLDER": jmx_folder,
        "TEST_FILES": ",".join(jmx_files),
    }

    try:
        resp = requests.post(jenkins_url, auth=(jenkins_user, jenkins_token), headers=headers, params=params)
        resp.raise_for_status()
        logger.info(f"[Jenkins Triggered] {resp.status_code} → {repo_url}@{branch}")
    except Exception as e:
        logger.exception(f"[Jenkins] Failed to trigger build")
        raise



@jenkins_bp.route("/trigger-jenkins", methods=["POST"])
@jwt_required()
def trigger_jenkins():
    import logging
    import os
    import requests
    from flask import request, jsonify, current_app
    from flask_jwt_extended import get_jwt_identity
    from urllib.parse import urlparse

    logger = logging.getLogger(__name__)
    logger.setLevel(logging.DEBUG)

    data = request.get_json()
    user_id = get_jwt_identity()

    repo_url = data.get("repo_url")
    branch = data.get("branch")
    jmx_folder = data.get("jmx_folder")
    test_files = data.get("test_files", [])

    if not all([repo_url, branch, jmx_folder]):
        logger.warning("Missing required fields")
        return jsonify({"error": "Missing fields"}), 400

    jenkins_base = os.getenv("JENKINS_BASE_URL", "https://jenkins.neeyatai.com")
    crumb_url = f"{jenkins_base}/crumbIssuer/api/json"
    jenkins_url = f"{jenkins_base}/job/NeeyatAI-JMeter-Pipeline/buildWithParameters"

    jenkins_user = os.getenv("JENKINS_USER")
    jenkins_token = os.getenv("JENKINS_API_TOKEN")

    if not jenkins_user or not jenkins_token:
        logger.error("Jenkins credentials not found in environment")
        return jsonify({"error": "Server misconfiguration"}), 500

    try:
        crumb_response = requests.get(
            crumb_url,
            auth=(jenkins_user, jenkins_token),
            timeout=5
        )
        crumb_response.raise_for_status()
        crumb_data = crumb_response.json()
        crumb_field = crumb_data["crumbRequestField"]
        crumb_value = crumb_data["crumb"]
    except Exception as e:
        logger.exception("Failed to get Jenkins crumb")
        return jsonify({"error": "Failed to get Jenkins crumb", "details": str(e)}), 500

    try:
        params = {
            "REPO_URL": repo_url,
            "BRANCH": branch,
            "USER_ID": user_id,
            "JMX_FOLDER": jmx_folder,
            "TEST_FILES": ",".join(test_files)
        }

        headers = {
            crumb_field: crumb_value,
            "Content-Type": "application/x-www-form-urlencoded",
        }

        response = requests.post(
            jenkins_url,
            auth=(jenkins_user, jenkins_token),
            headers=headers,
            params=params,
            timeout=10
        )

        logger.info(f"[Jenkins Trigger] {response.status_code}: {response.text}")

        if response.status_code == 201:
            from users.models import save_job_config

            save_job_config(
                repo_url=repo_url,
                branch=branch,
                user_id=user_id,
                jmx_folder=jmx_folder,
                jmx_files=test_files
            )

            import traceback

            try:
                def extract_repo_name(url):
                    # Should return like 'username/repo'
                    parts = urlparse(url).path.strip("/").split("/")
                    repo_path = f"{parts[-2]}/{parts[-1].replace('.git', '')}" if len(parts) >= 2 else None
                    current_app.logger.info(f"[Webhook Setup] Extracted repo path: {repo_path} from URL: {url}")
                    return repo_path

                repo_full_name = extract_repo_name(repo_url)
                if not repo_full_name:
                    current_app.logger.error(f"[Webhook Setup] Failed to extract repo name from {repo_url}")
                    raise ValueError("Invalid repo URL")

                backend_url = os.getenv("BACKEND_URL", "https://api.neeyatai.com")
                webhook_url = f"{backend_url}/github/setup-webhook"
                
                from flask_jwt_extended import create_access_token

                new_token = create_access_token(identity=user_id)
                auth_header = f"Bearer {new_token}"
                current_app.logger.info(f"[Webhook Setup] Generated internal token: {auth_header}")

                if not auth_header:
                    current_app.logger.warning("[Webhook Setup] Authorization header is missing in original request!")

                current_app.logger.info(f"[Webhook Setup] Forwarding Authorization header: {auth_header}")

                headers = {}
                cookies = {
                    "access_token_cookie": new_token
                }

                payload = {"repo": repo_full_name}

                current_app.logger.info(f"[Webhook Setup] Making POST request to {webhook_url}")
                current_app.logger.info(f"[Webhook Setup] Headers: {headers}")
                current_app.logger.info(f"[Webhook Setup] Payload: {payload}")

                webhook_resp = requests.post(webhook_url, json=payload, headers=headers, cookies=cookies, timeout=10)


                current_app.logger.info(f"[Webhook Setup] Response: {webhook_resp.status_code} - {webhook_resp.text}")

                if webhook_resp.status_code >= 300:
                    current_app.logger.error(f"[Webhook Setup] Failed to create webhook: {webhook_resp.text}")

            except Exception as hook_err:
                current_app.logger.exception(f"[Webhook Setup] Exception occurred:\n{traceback.format_exc()}")


            queue_url = response.headers.get("Location")  # 👈 Capture the queue URL
            return jsonify({
                "status": "success",
                "message": "Jenkins job triggered",
                "queue_url": queue_url
            }), 200

        return jsonify({
            "status": "error",
            "message": "Failed to trigger Jenkins job",
            "status_code": response.status_code,
            "jenkins_response": response.text
        }), 500

    except Exception as e:
        logger.exception("Exception during Jenkins trigger")
        return jsonify({"error": "Internal error", "details": str(e)}), 500

# --- Add this at the top of your file ---
import re
import json

ESSENTIAL_PATTERNS = [
    re.compile(r'\[Pipeline\] stage'),
    re.compile(r'Finished: (SUCCESS|FAILURE)'),
    re.compile(r'(ERROR|Exception|Caused by|Traceback)', re.IGNORECASE),
    re.compile(r'\[PROGRESS\] ::'),
    re.compile(r'(?i)(cloning|checking out|running jmeter|uploading report|report sent)'),
    re.compile(r'^\+ echo'),
]


def is_essential_log(line: str) -> bool:
    return any(p.search(line) for p in ESSENTIAL_PATTERNS)


def extract_progress_step(log_lines):
    for line in reversed(log_lines):
        if line.startswith("[PROGRESS] ::"):
            try:
                progress_data = json.loads(line.split("::", 1)[1].strip())
                return progress_data.get("step")
            except Exception:
                continue
    return None


@jenkins_bp.route("/progress", methods=["GET"])
@jwt_required()
def jenkins_progress():
    import time
    from urllib.parse import urljoin
    import requests
    from flask import jsonify, request
    import os

    queue_url = request.args.get("queue_url")
    if not queue_url:
        return jsonify({"error": "Missing queue_url"}), 400

    jenkins_base = os.getenv("JENKINS_BASE_URL", "https://jenkins.neeyatai.com")
    jenkins_user = os.getenv("JENKINS_USER")
    jenkins_token = os.getenv("JENKINS_API_TOKEN")

    def safe_get_json(url):
        try:
            resp = requests.get(url, auth=(jenkins_user, jenkins_token), timeout=10)
            resp.raise_for_status()
            return resp.json(), None
        except Exception as e:
            return None, {
                "error": f"Failed to fetch from {url}",
                "status_code": resp.status_code if 'resp' in locals() else None,
                "body": resp.text if 'resp' in locals() else str(e)
            }

    # Get queue item status
    queue_api = queue_url + "api/json"
    queue_data, error = safe_get_json(queue_api)
    if error:
        return jsonify({"error": "Queue API failed", **error}), 500

    # Wait for build to be assigned
    if "executable" not in queue_data:
        return jsonify({"status": "queued"})

    build_number = queue_data["executable"]["number"]
    job_name = queue_data["task"]["name"]
    build_url = f"{jenkins_base}/job/{job_name}/{build_number}/"

    # Get build status
    build_api = f"{build_url}api/json"
    build_info, error = safe_get_json(build_api)
    if error:
        return jsonify({"error": "Build API failed", **error}), 500

    # Get and filter logs
    try:
        console_url = f"{build_url}logText/progressiveText"
        log_resp = requests.get(console_url, auth=(jenkins_user, jenkins_token), timeout=10)
        log_resp.raise_for_status()
        raw_logs = log_resp.text.splitlines()
        filtered_logs = [line for line in raw_logs if is_essential_log(line)]
        logs = "\n".join(filtered_logs)
        current_step = extract_progress_step(raw_logs)
    except Exception as e:
        logs = f"[Log fetch failed] {str(e)}"
        current_step = None

    return jsonify({
        "status": "running" if build_info.get("building", False) else "completed",
        "build_number": build_number,
        "duration": build_info.get("estimatedDuration"),
        "url": build_url,
        "params": build_info.get("actions", [{}])[0].get("parameters", []),
        "logs": logs,
        "step": current_step or ("done" if not build_info.get("building", False) else None)
    })




import xml.etree.ElementTree as ET

@jenkins_bp.route("/estimate-vus", methods=["POST"])
def estimate_virtual_users():
    try:
        file = request.files.get("file")
        if not file:
            return jsonify({"error": "Missing JMX file"}), 400

        # Save the file temporarily
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jmx") as tmp:
            file.save(tmp.name)

            # Parse XML and look for VUs
            tree = ET.parse(tmp.name)
            root = tree.getroot()

            # Find ThreadGroup or similar component
            vus = 0
            for tg in root.iter("ThreadGroup"):
                for prop in tg.findall(".//stringProp[@name='ThreadGroup.num_threads']"):
                    vus += int(prop.text)

        os.remove(tmp.name)
        return jsonify({"vu_count": vus}), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@jenkins_bp.route("/check-quota", methods=["POST"])
def jenkins_check_quota():
    user_id = request.form.get("user_id")
    vu_count = int(request.form.get("vu_count", 0))

    user = find_user(user_id)
    if not user:
        return jsonify({"status": "error", "message": "Invalid user"}), 404

    remaining_info = get_remaining_virtual_users(user_id)
    remaining = remaining_info["remaining"]
    is_paid = user.get("paid_ends_at") and user["paid_ends_at"] > datetime.utcnow()
    limit = 1_000_000 if is_paid else 100

    if vu_count > remaining:
        return jsonify({
            "status": "denied",
            "message": f"Quota exceeded. Only {remaining} VUs left."
        }), 403

    return jsonify({"status": "ok", "remaining": remaining}), 200


@jenkins_bp.route("/generate-jtl-name", methods=["POST"])
def generate_jtl_name():
    try:
        file = request.files.get("file")
        if not file:
            return jsonify({"error": "Missing file"}), 400

        with tempfile.NamedTemporaryFile(delete=False, suffix=".jmx") as tmp:
            file.save(tmp.name)

        from extract_params_from_jmx import extract_editable_params
        params = extract_editable_params(tmp.name)
        tg = params.get("thread_groups", [{}])[0]

        tg_name = tg.get("name", "ThreadGroup").replace(" ", "_")
        num_threads = tg.get("num_threads", "NA")
        ramp_time = tg.get("ramp_time", "NA")
        loop_count = tg.get("loop_count", "NA")

        timestamp = datetime.now(timezone.utc).strftime("%d-%m-%Y_%H-%M-%S")
        filename = f"test_plan_{timestamp}_{tg_name}_{num_threads}_{ramp_time}_{loop_count}.jtl"

        return jsonify({"filename": filename})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500



@jenkins_bp.route("/github-triggered-run", methods=["POST"])
def github_triggered_run():
    from utils.pdf_generator import generate_pdf_report
    from jmeter_core import parse_jtl_summary
    try:
        user_id = request.form.get("user_id")
        repo_url = request.form.get("repo_url")
        test_name = request.form.get("test_name")
        vu_count = int(request.form.get("vu_count", 0))
        filename = request.form.get("filename")  # e.g., results.jtl
        file = request.files.get("file")

        if not all([user_id, repo_url, test_name, file, filename]):
            return jsonify({"error": "Missing fields"}), 400

        user = find_user(user_id)
        if not user:
            return jsonify({"error": "Invalid user"}), 404
        if not user.get("is_verified"):
            return jsonify({"error": "Unverified users are not allowed"}), 403

        # 📁 Save uploaded .jtl to a temp file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".jtl") as tmp_jtl:
            file.save(tmp_jtl.name)
            jtl_local_path = tmp_jtl.name

        # 📤 Upload original .jtl to S3
        jtl_s3_key = f"uploads/{user_id}/{filename}"
        upload_file_to_s3(jtl_local_path, jtl_s3_key)

        # 📝 Parse summary from JTL and generate PDF
        summary_json = parse_jtl_summary(jtl_local_path)  # This must return list[dict]
        pdf_filename = filename.rsplit(".", 1)[0] + ".pdf"
        local_pdf_path = os.path.join(tempfile.gettempdir(), pdf_filename)

        generate_pdf_report(summary_json, local_pdf_path, pdf_filename)

        # 📤 Upload PDF to S3 (same name, different extension)
        pdf_s3_key = f"uploads/{user_id}/{pdf_filename}"
        upload_file_to_s3(local_pdf_path, pdf_s3_key)

        # 📊 Update usage metrics
        increment_virtual_user_usage(user_id, vu_count)
        increment_user_metric(user_id, "total_tests_run")

        return jsonify({
            "status": "success",
            "jtl_filename": filename,
            "jtl_s3_key": jtl_s3_key,
            "pdf_filename": pdf_filename,
            "pdf_s3_key": pdf_s3_key
        }), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500




@jenkins_bp.route("/jenkins-email-report", methods=["POST"])
def jenkins_email_report():
    try:
        data = request.get_json()
        user_id = data.get("user_id")

        if not user_id:
            return jsonify({"error": "Missing user_id"}), 400

        test_name = data.get("test_name", "Unnamed Test")
        repo_url = data.get("repo_url", "Unknown Repo")
        jmx_results = data.get("jmx_results", [])
        attachments = data.get("attachments", [])  # Must include s3_key + filename

        if not jmx_results:
            return jsonify({"error": "Missing jmx_results"}), 400

        user = find_user(user_id)
        if not user:
            return jsonify({"error": "Invalid user"}), 404

        targets = [user_id] + user.get("additional_emails", [])

        # 📨 Compose summary of results
        result_lines = ""
        for res in jmx_results:
            reason = res.get("reason", "").strip() or "No error message provided."
            result_lines += (
                f"<li><b>{res['file']}</b>: <span style='color: {'green' if res['status'] == 'Success' else 'red'};'>"
                f"{res['status']}</span><br><i>{reason}</i></li>"
            )

        message = f"""
        <p><b>{test_name}</b> from repo <i>{repo_url}</i> has completed.</p>
        <ul>{result_lines}</ul>
        """

        # 📎 Download and attach files from S3
        from users.utils import download_file_from_s3
        import tempfile

        attachment_list = []
        for item in attachments:
            s3_key = item.get("s3_key")
            filename = item.get("filename")

            if not all([s3_key, filename]):
                logger.warning(f"Missing s3_key or filename in attachment item: {item}")
                continue

            try:
                tmp_path = os.path.join(tempfile.gettempdir(), filename)
                logger.info(f"📥 Downloading {filename} from {s3_key}")
                download_file_from_s3(s3_key, tmp_path)

                if not os.path.exists(tmp_path) or os.stat(tmp_path).st_size == 0:
                    logger.warning(f"[SKIP] Empty or missing file after download: {tmp_path}")
                    continue

                attachment_list.append((tmp_path, filename))
                logger.info(f"✅ Attached {filename}")
            except Exception as err:
                logger.exception(f"[ERROR] Could not attach {filename} from {s3_key}")


        # 🖋️ Render styled email
        body = styled_email_template(message=message)

        send_email(
            to=targets,
            subject=f"KickLoad Report - {test_name}",
            body=body,
            is_html=True,
            attachments=attachment_list
        )

        return jsonify({"status": "success", "message": "Email sent"}), 200

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

