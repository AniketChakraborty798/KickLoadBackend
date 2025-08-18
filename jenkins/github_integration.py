from flask import Blueprint, request, jsonify, current_app, redirect
import os, requests
from urllib.parse import urlencode, quote
from users.models import find_user, get_github_token, save_github_token, save_temp_state, get_userid_from_state, delete_github_token, delete_temp_state
from flask_jwt_extended import jwt_required, get_jwt_identity
import uuid

github_bp = Blueprint("github", __name__)

GITHUB_API_BASE = "https://api.github.com"
GITHUB_AUTH_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"

CLIENT_ID = os.getenv("GITHUB_CLIENT_ID")
CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET")
REDIRECT_URI = os.getenv("GITHUB_REDIRECT_URI")
SCOPES = os.getenv("GITHUB_SCOPES", "repo,user")

@github_bp.route("/github-webhook", methods=["POST"])
def github_webhook_listener():
    import hmac
    import hashlib
    import logging
    import os
    import traceback
    from flask import request, jsonify, abort

    logger = logging.getLogger("webhook")
    logger.setLevel(logging.INFO)

    secret = os.getenv("GITHUB_WEBHOOK_SECRET", "")

    # 🔐 Signature verification
    def verify_signature(payload, signature_header):
        if not signature_header:
            logger.warning("⚠️ Missing signature header.")
            return False

        try:
            algo, signature = signature_header.split('=')
            if algo != 'sha256':
                logger.warning(f"⚠️ Unexpected signature algorithm: {algo}")
                return False
            mac = hmac.new(secret.encode(), payload, hashlib.sha256)
            valid = hmac.compare_digest(mac.hexdigest(), signature)
            logger.info(f"🔍 Signature valid: {valid}")
            return valid
        except Exception as e:
            logger.exception("⚠️ Error during signature verification")
            return False

    signature_header = request.headers.get("X-Hub-Signature-256", "")
    raw_payload = request.get_data()

    if not verify_signature(raw_payload, signature_header):
        logger.warning("❌ Webhook signature invalid or missing")
        abort(400, "Invalid signature")

    try:
        event = request.headers.get("X-GitHub-Event", "")
        logger.info(f"📩 Received GitHub event: {event}")

        if event != "push":
            logger.info(f"ℹ️ Ignored non-push event: {event}")
            return jsonify({"status": "ignored", "reason": f"Unhandled event {event}"}), 200

        data = request.get_json()
        if not data:
            logger.warning("⚠️ No JSON payload received.")
            return jsonify({"status": "error", "reason": "Missing payload"}), 400

        repo_url_raw = data["repository"]["clone_url"]
        repo_url = repo_url_raw.rstrip(".git")  # 🔧 Normalize URL
        branch_ref = data["ref"]
        branch = branch_ref.split("/")[-1]

        logger.info(f"🔁 Push detected:")
        logger.info(f"    ├─ Raw repo URL: {repo_url_raw}")
        logger.info(f"    ├─ Normalized repo URL: {repo_url}")
        logger.info(f"    └─ Branch: {branch}")

        # 🔍 Lookup Jenkins job config
        from users.models import get_job_config
        job_config = get_job_config(repo_url, branch)


        if not job_config:
            from users.models import github_job_configs
            known = list(github_job_configs.find({}, {"_id": 0, "repo_url": 1, "branch": 1}))
            logger.warning("🛑 No job config matched.")
            logger.warning(f"    ├─ Searched for: {repo_url}@{branch}")
            logger.warning(f"    └─ Known configs:")
            for k in known:
                logger.warning(f"        - {k['repo_url']}@{k['branch']}")
            return jsonify({"status": "skipped", "reason": "No config found"}), 404


        logger.info(f"✅ Job config found for user: {job_config['user_id']}")

        # ✅ Trigger Jenkins pipeline
        from jenkins.jenkins_routes import trigger_jenkins_from_backend
        logger.info("🚀 Triggering Jenkins job...")
        trigger_jenkins_from_backend(
            repo_url=repo_url,
            branch=branch,
            user_id=job_config["user_id"],
            jmx_folder=job_config["jmx_folder"],
            jmx_files=job_config.get("jmx_files", [])
        )

        logger.info("✅ Jenkins triggered successfully.")
        return jsonify({"status": "success", "message": "Jenkins triggered"}), 200

    except Exception as e:
        logger.error(f"❌ Exception in webhook handler: {str(e)}")
        logger.debug(traceback.format_exc())
        return jsonify({"error": "Internal server error", "details": str(e)}), 500



@github_bp.route("/setup-webhook", methods=["POST"])
@jwt_required()
def setup_github_webhook():
    from urllib.parse import urlparse

    data = request.get_json()
    user_id = get_jwt_identity()

    repo_full_name = data.get("repo")  # eg: 'username/repo'
    if not repo_full_name:
        return jsonify({"error": "Missing repo"}), 400

    token = get_user_token(user_id)
    if not token:
        return jsonify({"error": "GitHub not connected"}), 401

    webhook_url = os.getenv("GITHUB_WEBHOOK_URL", "https://api.neeyatai.com/github/github-webhook")  # ✅ set this in env

    try:
        # 1. List existing webhooks
        hooks_url = f"{GITHUB_API_BASE}/repos/{repo_full_name}/hooks"
        hooks_resp = requests.get(hooks_url, headers=github_headers(token))
        if hooks_resp.status_code != 200:
            return jsonify({"error": "Failed to fetch webhooks", "details": hooks_resp.text}), 400

        existing_hooks = hooks_resp.json()

        # 2. Delete matching webhook if exists
        for hook in existing_hooks:
            if hook["config"].get("url") == webhook_url:
                del_url = f"{GITHUB_API_BASE}/repos/{repo_full_name}/hooks/{hook['id']}"
                del_resp = requests.delete(del_url, headers=github_headers(token))
                if del_resp.status_code != 204:
                    current_app.logger.warning(f"Failed to delete webhook: {del_resp.text}")

        # 3. Create new webhook
        create_resp = requests.post(
            hooks_url,
            headers=github_headers(token),
            json={
                "name": "web",
                "active": True,
                "events": ["push"],
                "config": {
                    "url": webhook_url,
                    "content_type": "json",
                    "insecure_ssl": "0",
                    "secret": os.getenv("GITHUB_WEBHOOK_SECRET", "")  # <-- Add this
                }

            }
        )

        if create_resp.status_code >= 300:
            return jsonify({"error": "Failed to create webhook", "details": create_resp.text}), 400

        return jsonify({"status": "success", "message": "Webhook created"}), 200

    except Exception as e:
        current_app.logger.exception("Webhook setup error")
        return jsonify({"error": "Server error", "details": str(e)}), 500






# Step 1: Frontend redirects here
@github_bp.route("/connect")
@jwt_required()
def github_connect():
    user_id = get_jwt_identity()
    user = find_user(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 400

    state = str(uuid.uuid4())
    save_temp_state(state, user_id)

    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
        "allow_signup": "true"
    }

    if request.args.get("force") == "true":
        delete_github_token(user_id)
        params["prompt"] = "login"  # 💡 Force fresh login UI

    return redirect(f"{GITHUB_AUTH_URL}?{urlencode(params)}")




# Step 2: GitHub redirects back here after login
from itsdangerous import URLSafeSerializer

@github_bp.route("/callback")
def github_callback():
    error = request.args.get("error")
    error_description = request.args.get("error_description")
    state = request.args.get("state")
    code = request.args.get("code")

    if error == "access_denied":
        current_app.logger.warning(f"GitHub OAuth denied: {error_description} (state: {state})")

        user_id = get_userid_from_state(state)
        if user_id:
            delete_github_token(user_id)  # Clean old session

        return redirect("https://github.com/")

    if not code or not state:
        return jsonify({"error": "Missing code or state"}), 400

    user_id = get_userid_from_state(state)

    if not user_id:
        current_app.logger.warning(f"Callback called with invalid/expired state: {state}")
        return redirect("https://kickload.neeyatai.com/github/error?message=Invalid%20or%20expired%20session")

    try:
        # Exchange code for access token
        token_resp = requests.post(
            GITHUB_TOKEN_URL,
            headers={"Accept": "application/json"},
            data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "code": code,
                "redirect_uri": REDIRECT_URI,
                "state": state,
            },
            timeout=10,
        )

        if token_resp.status_code != 200:
            current_app.logger.error(f"GitHub token exchange failed: {token_resp.text}")
            return jsonify({"error": "GitHub token exchange failed"}), 400

        token_data = token_resp.json()
        access_token = token_data.get("access_token")
        if not access_token:
            return jsonify({"error": "Missing access token from GitHub"}), 400

        # Fetch GitHub user info
        user_info_resp = requests.get(
            f"{GITHUB_API_BASE}/user",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10
        )
        if user_info_resp.status_code != 200:
            return jsonify({"error": "Failed to fetch GitHub user info"}), 400

        user_info = user_info_resp.json()
        github_username = user_info.get("login")
        if not github_username:
            return jsonify({"error": "GitHub username not found"}), 400

        # Save GitHub token & metadata
        save_github_token(user_id, {
            "access_token": access_token,
            "github_username": github_username,
            "scope": token_data.get("scope"),
            "token_type": token_data.get("token_type")
        })

        # Remove state so it can't be reused
        delete_temp_state(user_id)

        # Generate frontend token
        s = URLSafeSerializer(current_app.config["SECRET_KEY"])
        frontend_token = s.dumps({"user_id": user_id})

        return redirect(f"https://kickload.neeyatai.com/github/success?token={frontend_token}")

    except Exception as e:
        current_app.logger.exception("GitHub OAuth callback error")
        return jsonify({"error": "Server error", "details": str(e)}), 500



# Helper to get access token from stored GitHub token DB
def get_user_token(user_id):
    github_token_data = get_github_token(user_id)
    return github_token_data.get("access_token") if github_token_data else None



def github_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }

@github_bp.route("/repos", methods=["GET"])
@jwt_required()
def list_repos():
    user_id = get_jwt_identity()
    token = get_user_token(user_id)
    if not token:
        return jsonify({"error": "GitHub not connected"}), 401
    url = f"{GITHUB_API_BASE}/user/repos"
    resp = requests.get(url, headers=github_headers(token))
    return jsonify(resp.json()), resp.status_code

@github_bp.route("/branches", methods=["GET"])
@jwt_required()
def list_branches():
    user_id = get_jwt_identity()
    repo = request.args.get("repo")
    token = get_user_token(user_id)
    if not token or not repo:
        return jsonify({"error": "Missing repo or token"}), 400
    url = f"{GITHUB_API_BASE}/repos/{repo}/branches"
    resp = requests.get(url, headers=github_headers(token))
    return jsonify(resp.json()), resp.status_code

@github_bp.route("/contents", methods=["GET"])
@jwt_required()
def list_repo_contents():
    user_id = get_jwt_identity()
    repo = request.args.get("repo")
    path = request.args.get("path", "")
    branch = request.args.get("branch", "main")
    token = get_user_token(user_id)
    if not token or not repo:
        return jsonify({"error": "Missing repo or token"}), 400
    url = f"{GITHUB_API_BASE}/repos/{repo}/contents/{path}?ref={branch}"
    resp = requests.get(url, headers=github_headers(token))
    return jsonify(resp.json()), resp.status_code

@github_bp.route("/token", methods=["GET"])
@jwt_required()
def get_token_debug():
    user_id = get_jwt_identity()
    token = get_user_token(user_id)
    if token:
        return jsonify({"token": token}), 200
    return jsonify({"error": "Not connected"}), 401



@github_bp.route("/disconnect", methods=["POST"])
@jwt_required()
def github_disconnect():
    user_id = get_jwt_identity()

    try:
        delete_github_token(user_id)
        delete_temp_state(user_id)  # ✅ Now defined!

        logout_url = "https://github.com/logout?return_to=https://kickload.neeyatai.com/github/disconnected"

        return jsonify({
            "status": "disconnected",
            "github_logout_url": logout_url
        }), 200

    except Exception as e:
        current_app.logger.exception("Error during GitHub disconnect")
        return jsonify({"error": "Failed to disconnect", "details": str(e)}), 500


