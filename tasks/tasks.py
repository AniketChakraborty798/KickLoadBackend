from tasks.celery import celery, shared_task
from email_utils import _send_email_internal
from users.scheduler import check_expiry
from gemini import generate_with_gemini


@celery.task
def send_email_async(to, subject, body, attachments=None, is_html=False):
    return _send_email_internal(to, subject, body, attachments, is_html)

@celery.task
def run_jmeter_test_async(s3_key):
    import os, uuid, shutil
    import sys
    sys.path.append('/app')

    from users.utils import download_file_from_s3, upload_file_to_s3
    from jmeter_core import run_jmeter_internal

    # Derive user-safe identifier for scratch dir
    user_prefix = os.path.dirname(s3_key) + "/"  # e.g., uploads/user_email/
    user_id = user_prefix.strip("/").replace("/", "_")  # e.g., uploads_user_email
    uid = uuid.uuid4().hex[:8]
    temp_dir = os.path.join("/tmp/jmeter", f"{user_id}_{uid}")
    os.makedirs(temp_dir, exist_ok=True)

    try:
        jmx_filename = os.path.basename(s3_key)
        jtl_filename = jmx_filename.replace(".jmx", ".jtl")

        local_jmx_path = os.path.join(temp_dir, jmx_filename)
        local_result_path = os.path.join(temp_dir, jtl_filename)

        # Download the .jmx file from S3
        download_file_from_s3(s3_key, local_jmx_path)
        if not os.path.exists(local_jmx_path):
            raise FileNotFoundError(f"Downloaded .jmx not found: {local_jmx_path}")

        # Run JMeter
        summary_output = run_jmeter_internal(local_jmx_path, local_result_path)

        # Upload .jtl result to S3
        result_key = os.path.join(user_prefix, jtl_filename)
        upload_file_to_s3(local_result_path, result_key)

        return summary_output

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)



@celery.task
def check_expiry_task():
    check_expiry(loop=False)

@shared_task
def generate_gemini_analysis_async(prompt):
    return generate_with_gemini(prompt)

