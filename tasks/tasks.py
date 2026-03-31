from tasks.celery import celery, shared_task
from jmeter_core import run_jmeter_internal
from email_utils import _send_email_internal
from users.scheduler import check_expiry
from gemini import generate_with_gemini
import redis
import os, uuid, shutil, tempfile, json

redis_client = redis.StrictRedis(
    host="redis",
    port=6379,
    password=os.getenv("REDIS_PASSWORD"),
    decode_responses=True
)

@celery.task
def validate_jmx_task(xml_content: str, timeout: int = 60) -> dict:
    import tempfile
    import os
    import subprocess
    import logging
    from jmeter_core import JMETER_BIN, MINIMAL_PROPERTIES_CONTENT, apply_overrides_to_jmx

    logger = logging.getLogger(__name__)

    try:
        with tempfile.TemporaryDirectory() as tempdir:
            original_jmx_path = os.path.join(tempdir, "original_test.jmx")
            overridden_jmx_path = os.path.join(tempdir, "overridden_test.jmx")

            with open(original_jmx_path, "w", encoding="utf-8") as f:
                f.write(xml_content)

            # No CSV download here for lightweight validation

            # Apply overrides to ensure at least 1 thread and 1 loop
            overrides = {
                "num_threads": "1",
                "loop_count": "1",
                "ramp_time": "1"
            }
            apply_overrides_to_jmx(original_jmx_path, overridden_jmx_path, overrides)

            properties_path = os.path.join(tempdir, "minimal.properties")
            with open(properties_path, "w", encoding="utf-8") as f:
                f.write(MINIMAL_PROPERTIES_CONTENT.strip())

            results_path = os.path.join(tempdir, "results.jtl")

            cmd = [
                JMETER_BIN,
                "-n",
                "-t", overridden_jmx_path,
                "-l", results_path,
                "-q", properties_path
            ]

            env = {**os.environ, "JVM_ARGS": "-Dlog4j.configurationFile=/opt/jmeter/bin/log4j2-silent.xml"}


            completed_process = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env
            )

            if completed_process.returncode == 0:
                if os.path.exists(results_path):
                    return {"valid": True, "message": "JMeter validation and lightweight run passed."}
                else:
                    # Return JMeter stdout/stderr to aid debugging why no results were generated
                    return {
                        "valid": False,
                        "message": (
                            "JMeter run succeeded but no results file was generated. "
                            f"JMeter STDOUT:\n{completed_process.stdout}\n"
                            f"JMeter STDERR:\n{completed_process.stderr}\n"
                            "Common causes: zero threads or loops, missing samplers, or ResultCollector misplacement."
                        )
                    }
            else:
                return {
                    "valid": False,
                    "message": (
                        f"JMeter exited with code {completed_process.returncode}.\n"
                        f"STDOUT:\n{completed_process.stdout}\n"
                        f"STDERR:\n{completed_process.stderr}"
                    )
                }

    except Exception as e:
        logger.exception(f"Exception running validation: {e}")
        return {"valid": False, "message": f"Exception running validation: {e}"}



@celery.task
def send_email_async(to, subject, body, attachments=None, is_html=False):
    return _send_email_internal(to, subject, body, attachments, is_html)


@celery.task
def run_jmeter_distributed_test_async(s3_key, overrides=None, worker_ips=None, user_email=None):
    from datetime import datetime, timezone
    from users.utils import download_file_from_s3, upload_file_to_s3
    from jmeter_core import apply_overrides_to_jmx, download_required_csvs, parse_jtl_summary, sanitize_log_line
    from utils.pdf_generator import generate_pdf_report
    import logging
    import os
    import shutil
    import uuid
    import json
    from asgutils.asg import scale_asg_down_to_zero

    logger = logging.getLogger(__name__)
    logger.info(f"🏃 Starting Distributed JMeter test task for s3_key: {s3_key}")

    overrides = overrides or {}
    test_start_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    user_prefix = os.path.dirname(s3_key) + "/"
    user_id = user_prefix.strip("/").replace("/", "_")
    uid = uuid.uuid4().hex[:8]
    temp_dir = os.path.join("/tmp/jmeter", f"{user_id}_{uid}")
    os.makedirs(temp_dir, exist_ok=True)

    try:
        jmx_filename = os.path.basename(s3_key)
        local_jmx_path = os.path.join(temp_dir, jmx_filename)
        temp_jmx_path = os.path.join(temp_dir, f"temp_{jmx_filename}")

        logger.info(f"⬇️ Downloading JMX file from S3: {s3_key} → {local_jmx_path}")
        download_file_from_s3(s3_key, local_jmx_path)
        if not os.path.exists(local_jmx_path):
            logger.error(f"❌ JMX file not found after download: {local_jmx_path}")
            raise FileNotFoundError(f"Downloaded .jmx not found: {local_jmx_path}")
        logger.info(f"✅ JMX file downloaded: {local_jmx_path}")

        if overrides:
            logger.info(f"🛠 Applying overrides: {overrides}")
            apply_overrides_to_jmx(local_jmx_path, temp_jmx_path, overrides)
            jmx_path_to_use = temp_jmx_path
            logger.info(f"✅ Overrides applied. Using temp JMX: {temp_jmx_path}")
        else:
            jmx_path_to_use = local_jmx_path

        # CSV download for dependencies
        download_required_csvs(jmx_path_to_use, user_prefix, temp_dir)

        try:
            params = extract_editable_params(jmx_path_to_use)
            tg = params.get("thread_groups", [{}])[0]
            tg_name = tg.get("name", "ThreadGroup").replace(" ", "_")
            num_threads = tg.get("num_threads", "NA")
            ramp_time = tg.get("ramp_time", "NA")
            loop_count = tg.get("loop_count", "NA")
        except Exception as e:
            logger.warning(f"⚠️ Failed to extract thread group params: {e}")
            tg_name = "UnknownTG"
            num_threads = ramp_time = loop_count = "NA"

        timestamp = datetime.now(timezone.utc).strftime("%d-%m-%Y_%H-%M-%S")
        base_filename = f"test_plan_{timestamp}_{tg_name}_{num_threads}_{ramp_time}_{loop_count}"
        jtl_filename = f"{base_filename}.jtl"
        pdf_filename = f"{base_filename}.pdf"

        local_result_path = os.path.join(temp_dir, jtl_filename)
        local_pdf_path = os.path.join(temp_dir, pdf_filename)

        log_channel = run_jmeter_distributed_test_async.request.id

        # Run distributed JMeter master
        JMETER_BIN = "/opt/apache-jmeter-5.6.3/bin/jmeter"
        remote_ips = ",".join(worker_ips)
        properties_path = os.path.join(temp_dir, "minimal.properties")
        with open(properties_path, "w") as f:
            f.write(MINIMAL_PROPERTIES_CONTENT.strip())
        cmd = [
            JMETER_BIN,
            "-n",
            "-t", jmx_path_to_use,
            "-R", remote_ips,
            "-l", local_result_path,
            "-q", properties_path
        ]
        logger.info(f"🚀 Running Distributed JMeter: {' '.join(cmd)}")
        import subprocess
        import sys
        with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1) as process:
            full_output = ""
            try:
                for line in iter(process.stdout.readline, ''):
                    full_output += line
                    if log_channel:
                        safe_line = sanitize_log_line(line.strip())
                        if safe_line:
                            redis_client.publish(log_channel, safe_line)
                    sys.stdout.flush()
            except Exception as e:
                logger.warning(f"⚠️ Error while reading subprocess output: {e}")
            leftover = process.stdout.read()
            if leftover:
                full_output += leftover
                if log_channel:
                    redis_client.publish(log_channel, leftover.strip())
            retcode = process.wait(timeout=int(os.getenv("JMETER_TIMEOUT", 1200)))
            if retcode != 0:
                raise RuntimeError("JMeter distributed test exited with non-zero status.")

        # S3 result upload + PDF as before
        result_key = os.path.join(user_prefix, jtl_filename)
        upload_file_to_s3(local_result_path, result_key)
        pdf_key = os.path.join(user_prefix, pdf_filename)

        try:
            summary_json = parse_jtl_summary(local_result_path)
            test_end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            generate_pdf_report(
                summary_json,
                local_pdf_path,
                pdf_filename,
                grafana_from_ms=test_start_ms,
                grafana_to_ms=test_end_ms,
            )
            if os.path.exists(local_pdf_path):
                upload_file_to_s3(local_pdf_path, pdf_key)
                logger.info(f"📤 Uploaded PDF to S3: {pdf_key}")
            else:
                logger.error(f"❌ PDF file missing after generation!")
        except Exception as e:
            logger.exception(f"❌ Failed to generate/upload PDF: {e}")

        # Observability: emit metrics to InfluxDB (best-effort; should never fail the task)
        try:
            try:
                from observability.influx import write_jmeter_summary_to_influx
            except ModuleNotFoundError:
                import importlib.util

                module_path = os.path.abspath(
                    os.path.join(os.path.dirname(__file__), "..", "observability", "influx.py")
                )
                spec = importlib.util.spec_from_file_location("influx", module_path)
                if spec is None or spec.loader is None:
                    raise
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                write_jmeter_summary_to_influx = module.write_jmeter_summary_to_influx

            if "summary_json" in locals() and summary_json:
                write_jmeter_summary_to_influx(
                    summary_rows=summary_json,
                    task_id=run_jmeter_distributed_test_async.request.id,
                    user_email=user_email,
                    jmx_filename=jmx_filename,
                    jtl_filename=jtl_filename,
                )
        except Exception as e:
            logger.warning(f"Influx write skipped/failed: {e}")

        # Auto scale down
        scale_asg_down_to_zero()

        logger.info(f"✅ Distributed Task complete. Returning metadata to frontend.")
        return {
            "summary": summary_json,
            "filename": jtl_filename,
            "pdf_filename": pdf_filename,
            "jmx_filename": jmx_filename,
            "log_channel": log_channel
        }
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)




@celery.task
def run_jmeter_test_async(s3_key, overrides=None, user_email=None):

    from datetime import datetime, timezone
    from users.utils import download_file_from_s3, upload_file_to_s3
    from jmeter_core import run_jmeter_internal, apply_overrides_to_jmx, download_required_csvs
    from extract_params_from_jmx import extract_editable_params
    from utils.pdf_generator import generate_pdf_report
    import logging

    logger = logging.getLogger(__name__)
    logger.info(f"🏃 Starting JMeter test task for s3_key: {s3_key}")

    overrides = overrides or {}
    test_start_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    user_prefix = os.path.dirname(s3_key) + "/"
    user_id = user_prefix.strip("/").replace("/", "_")
    uid = uuid.uuid4().hex[:8]
    temp_dir = os.path.join("/tmp/jmeter", f"{user_id}_{uid}")
    os.makedirs(temp_dir, exist_ok=True)

    try:
        jmx_filename = os.path.basename(s3_key)
        local_jmx_path = os.path.join(temp_dir, jmx_filename)
        temp_jmx_path = os.path.join(temp_dir, f"temp_{jmx_filename}")

        logger.info(f"⬇️ Downloading JMX file from S3: {s3_key} → {local_jmx_path}")
        download_file_from_s3(s3_key, local_jmx_path)
        if not os.path.exists(local_jmx_path):
            logger.error(f"❌ JMX file not found after download: {local_jmx_path}")
            raise FileNotFoundError(f"Downloaded .jmx not found: {local_jmx_path}")
        logger.info(f"✅ JMX file downloaded: {local_jmx_path}")

        # Apply overrides if present
        if overrides:
            logger.info(f"🛠 Applying overrides: {overrides}")
            apply_overrides_to_jmx(local_jmx_path, temp_jmx_path, overrides)
            jmx_path_to_use = temp_jmx_path
            logger.info(f"✅ Overrides applied. Using temp JMX: {temp_jmx_path}")
        else:
            jmx_path_to_use = local_jmx_path

        # Extract thread group parameters for naming
        try:
            params = extract_editable_params(jmx_path_to_use)
            tg = params.get("thread_groups", [{}])[0]
            tg_name = tg.get("name", "ThreadGroup").replace(" ", "_")
            num_threads = tg.get("num_threads", "NA")
            ramp_time = tg.get("ramp_time", "NA")
            loop_count = tg.get("loop_count", "NA")
        except Exception as e:
            logger.warning(f"⚠️ Failed to extract thread group params: {e}")
            tg_name = "UnknownTG"
            num_threads = ramp_time = loop_count = "NA"

        timestamp = datetime.now(timezone.utc).strftime("%d-%m-%Y_%H-%M-%S")
        base_filename = f"test_plan_{timestamp}_{tg_name}_{num_threads}_{ramp_time}_{loop_count}"
        jtl_filename = f"{base_filename}.jtl"
        pdf_filename = f"{base_filename}.pdf"

        local_result_path = os.path.join(temp_dir, jtl_filename)
        local_pdf_path = os.path.join(temp_dir, pdf_filename)

        logger.info(f"📦 Preparing result paths: JTL → {local_result_path}, PDF → {local_pdf_path}")


        download_required_csvs(jmx_path_to_use, user_prefix, temp_dir)
        logger.info(f"📥 CSV dependencies (if any) downloaded to {temp_dir}")

        log_channel = run_jmeter_test_async.request.id
        logger.info(f"🚀 Running JMeter test, log_channel={log_channel}")

        # Log the final JMX that will be used to run JMeter (for diagnostics)
        try:
            with open(jmx_path_to_use, 'r', encoding='utf-8') as jmx_file:
                final_jmx_content = jmx_file.read()
            logger.info(f"📝 FINAL JMX ({jmx_path_to_use}):\n{final_jmx_content}")
        except Exception as e:
            logger.error(f"❗ Failed to log final JMX file: {e}")

        summary_output = run_jmeter_internal(jmx_path_to_use, local_result_path, log_channel=log_channel, task_id=run_jmeter_test_async.request.id,
        num_threads=num_threads,
        user_email=user_email)

        if os.path.exists(local_result_path):
            logger.info(f"✅ JTL file generated: {local_result_path}")
        else:
            logger.error(f"❌ JTL file missing after test run!")

        result_key = os.path.join(user_prefix, jtl_filename)
        upload_file_to_s3(local_result_path, result_key)
        logger.info(f"📤 Uploaded JTL to S3: {result_key}")

        pdf_key = os.path.join(user_prefix, pdf_filename)
        

        try:
            summary_json = json.loads(summary_output) if isinstance(summary_output, str) else summary_output
            logger.info(f"🧾 Generating PDF report from summary...")

            test_end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            generate_pdf_report(
                summary_json,
                local_pdf_path,
                pdf_filename,
                grafana_from_ms=test_start_ms,
                grafana_to_ms=test_end_ms,
            )
            if os.path.exists(local_pdf_path):
                logger.info(f"✅ PDF generated: {local_pdf_path}")
                upload_file_to_s3(local_pdf_path, pdf_key)
                logger.info(f"📤 Uploaded PDF to S3: {pdf_key}")

                
            else:
                logger.error(f"❌ PDF file missing after generation!")

        except Exception as e:
            logger.exception(f"❌ Failed to generate/upload PDF: {e}")
            
        # Observability: emit metrics to InfluxDB (best-effort; should never fail the task)
        try:
            try:
                from observability.influx import write_jmeter_summary_to_influx
            except ModuleNotFoundError:
                import importlib.util

                module_path = os.path.abspath(
                    os.path.join(os.path.dirname(__file__), "..", "observability", "influx.py")
                )
                spec = importlib.util.spec_from_file_location("influx", module_path)
                if spec is None or spec.loader is None:
                    raise
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                write_jmeter_summary_to_influx = module.write_jmeter_summary_to_influx

            if "summary_json" in locals() and summary_json:
                write_jmeter_summary_to_influx(
                    summary_rows=summary_json,
                    task_id=run_jmeter_test_async.request.id,
                    user_email=user_email,
                    jmx_filename=jmx_filename,
                    jtl_filename=jtl_filename,
                )
        except Exception as e:
            logger.warning(f"Influx write skipped/failed: {e}")


        logger.info(f"✅ Task complete. Returning metadata to frontend.")
        return {
            "summary": summary_output,
            "filename": jtl_filename,
            "pdf_filename": pdf_filename,
            "jmx_filename": jmx_filename,
            "log_channel": log_channel
        }

    finally:
        logger.info(f"🧹 Cleaning up temporary directory: {temp_dir}")
        shutil.rmtree(temp_dir, ignore_errors=True)

 


@celery.task
def check_expiry_task():
    check_expiry(loop=False)

@shared_task
def generate_gemini_analysis_async(prompt):
    return generate_with_gemini(prompt)


@celery.task
def compare_jtls_with_gemini_async(email, filenames):
    import os
    import tempfile
    from users.utils import download_file_from_s3, upload_file_to_s3
    from compare_utils import (
        build_gemini_prompt,
        generate_comparison_pdf,
        clean_output_text,
        build_html_report_from_plain_text
    )
    from jmeter_core import parse_jtl_summary
    from datetime import datetime, timezone

    def convert_summary_for_gemini(item):
        return {
            "label": item["label"],
            "samples": item["samples"],
            "avg_response_time": item["average_ms"],
            "min_response_time": item["min_ms"],
            "max_response_time": item["max_ms"],
            "error_count": int(item["samples"] * item["error_pct"] / 100.0)
        }

    user_prefix = f"uploads/{email}/"
    summaries = []
    middle_names = []

    with tempfile.TemporaryDirectory() as temp_dir:
        for jtl in filenames:
            local_path = os.path.join(temp_dir, jtl)
            s3_key = f"{user_prefix}{jtl}"
            if not download_file_from_s3(s3_key, local_path):
                raise Exception(f"Failed to download {jtl} from S3")

            parsed = parse_jtl_summary(local_path)
            total_row = next((item for item in parsed if item["label"] == "TOTAL"), None)
            if not total_row:
                raise Exception(f"No TOTAL summary found in {jtl}")

            summary = convert_summary_for_gemini(total_row)

            # Extract clean name
            middle = jtl[len("test_plan_"):-4] if jtl.startswith("test_plan_") and jtl.endswith(".jtl") else jtl.replace(".jtl", "")
            summary["label"] = middle
            trimmed = middle[:10]
            middle_names.append(trimmed)
            summaries.append(summary)

        # Build prompt and get Gemini response
        prompt = build_gemini_prompt(summaries)
        response = generate_with_gemini(prompt)

        if not response:
            raise Exception("Gemini returned empty response")

        cleaned_summary = clean_output_text(response)

        # Prepare output filename
        timestamp = datetime.now(timezone.utc).strftime("%d-%m-%Y_%H-%M-%S")
        name_part = "_vs_".join(middle_names)
        pdf_filename = f"compare_{timestamp}_{name_part}.pdf"
        pdf_path = os.path.join(temp_dir, pdf_filename)

        generate_comparison_pdf(cleaned_summary, pdf_path, summaries, pdf_filename)

        # Upload to S3
        upload_file_to_s3(pdf_path, f"{user_prefix}{pdf_filename}")

        html_report = build_html_report_from_plain_text(cleaned_summary, summaries)

        return {
            "status": "success",
            "filename": pdf_filename,
            "summary": cleaned_summary,
            "html_report": html_report
        }



@celery.task
def cleanup_expired_trials_task():
    from datetime import datetime, timedelta
    from pymongo import MongoClient
    from users.utils import s3, BUCKET_NAME
    import os

    mongo_uri = os.getenv("MONGO_URI")
    mongo_db_name = os.getenv("MONGO_DB_NAME")
    client = MongoClient(mongo_uri)
    db = client[mongo_db_name]
    users_collection = db["users"]

    def delete_s3_folder(user_email):
        prefix = f"uploads/{user_email}/"
        paginator = s3.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=BUCKET_NAME, Prefix=prefix)

        to_delete = []
        for page in pages:
            for obj in page.get("Contents", []):
                to_delete.append({'Key': obj['Key']})

        if to_delete:
            s3.delete_objects(Bucket=BUCKET_NAME, Delete={'Objects': to_delete})
            print(f"✅ Deleted {len(to_delete)} files for user {user_email}")
        else:
            print(f"ℹ️ No files to delete for user {user_email}")

    cutoff = datetime.utcnow() - timedelta(days=15)

    users = users_collection.find({
        "paid_ends_at": None,
        "$or": [
            {"trial_ends_at": {"$lte": cutoff}},
            {"trial_ends_at": None, "created_at": {"$lte": cutoff}}
        ]
    })

    for user in users:
        email = user.get("email")
        if not email:
            print("⚠️ User missing email, skipping...")
            continue
        print(f"🔍 Checking user: {email}")
        delete_s3_folder(email)

