import os
import subprocess
import logging
import time
import uuid
import shutil

logger = logging.getLogger(__name__)

import redis
redis_client = redis.StrictRedis(host="redis", port=6379, password=os.getenv("REDIS_PASSWORD"), decode_responses=True)

JMETER_BIN = "/opt/apache-jmeter-5.6.3/bin/jmeter"


import openpyxl
import csv

def convert_xlsx_to_csv(xlsx_path, csv_path):
    wb = openpyxl.load_workbook(xlsx_path)
    sheet = wb.active

    with open(csv_path, "w", newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        for row in sheet.iter_rows(values_only=True):
            writer.writerow(row)



def download_required_csvs(jmx_path, s3_prefix, download_dir):
    import xml.etree.ElementTree as ET
    from users.utils import download_file_from_s3
    import os

    tree = ET.parse(jmx_path)
    root = tree.getroot()

    updated = False

    for csv_data_set in root.iter("CSVDataSet"):
        file_prop = csv_data_set.find(".//stringProp[@name='filename']")
        if file_prop is not None and file_prop.text:
            original_filename = file_prop.text.strip()
            ext = os.path.splitext(original_filename)[1].lower()
            base_name = os.path.basename(original_filename)
            s3_key = os.path.join(s3_prefix, base_name)

            local_path = os.path.join(download_dir, base_name)
            download_file_from_s3(s3_key, local_path)

            # If it's an .xlsx, convert to .csv and update the JMX reference
            if ext == ".xlsx":
                csv_filename = os.path.splitext(base_name)[0] + ".csv"
                csv_path = os.path.join(download_dir, csv_filename)

                convert_xlsx_to_csv(local_path, csv_path)

                # Update the filename in the JMX to use .csv
                file_prop.text = csv_filename
                updated = True

    # Write the updated JMX if needed
    if updated:
        tree.write(jmx_path, encoding="utf-8", xml_declaration=True)


def set_prop(elem, name, value):
    for tag in ["stringProp", "intProp", "longProp"]:
        node = elem.find(f".//{tag}[@name='{name}']")
        if node is not None:
            node.text = str(value)
            return True
    return False


def apply_overrides_to_jmx(original_path, output_path, overrides):
    import xml.etree.ElementTree as ET
    tree = ET.parse(original_path)
    root = tree.getroot()

    for tg in root.iter("ThreadGroup"):
        if "num_threads" in overrides:
            set_prop(tg, "ThreadGroup.num_threads", overrides["num_threads"])
        if "ramp_time" in overrides:
            set_prop(tg, "ThreadGroup.ramp_time", overrides["ramp_time"])
        if "loop_count" in overrides:
            set_prop(tg, "LoopController.loops", overrides["loop_count"])


    tree.write(output_path, encoding="utf-8", xml_declaration=True)


MINIMAL_PROPERTIES_CONTENT = """
jmeter.save.saveservice.output_format=xml
jmeter.save.saveservice.assertion_results=none
jmeter.save.saveservice.bytes=true
jmeter.save.saveservice.latency=true
jmeter.save.saveservice.label=true
jmeter.save.saveservice.response_code=true
jmeter.save.saveservice.response_message=true
jmeter.save.saveservice.successful=true
jmeter.save.saveservice.thread_counts=true
jmeter.save.saveservice.time=true
"""


from collections import defaultdict
import xml.etree.ElementTree as ET
from statistics import mean, stdev

def parse_jtl_summary(jtl_path):
    tree = ET.parse(jtl_path)
    root = tree.getroot()

    summary = defaultdict(lambda: {
        "samples": [],
        "total_bytes": 0,
        "sent_bytes": 0,
        "success_count": 0
    })

    def parse_sample_node(node, parent_label=None):
        # Some samples have "lb" attribute (label)
        label = node.get("lb") or parent_label or "Unknown"
        elapsed = int(node.get("t", 0))  # ms
        success = node.get("s") == "true"
        received = int(node.get("by", 0))
        sent = int(node.get("sby", 0))

        # Record sample in summary by label
        summary[label]["samples"].append(elapsed)
        summary[label]["total_bytes"] += received
        summary[label]["sent_bytes"] += sent
        if success:
            summary[label]["success_count"] += 1

        # Recursively parse children if they exist (nested samples)
        for child in node:
            if child.tag in ("sample", "httpSample"):
                parse_sample_node(child, parent_label=label)

    for child in root:
        if child.tag in ("sample", "httpSample"):
            parse_sample_node(child)

    result = []

    for label, data in summary.items():
        samples = data["samples"]
        count = len(samples)
        duration_sec = sum(samples) / 1000 if count else 1

        result.append({
            "label": label,
            "samples": count,
            "average_ms": round(mean(samples), 2),
            "min_ms": min(samples),
            "max_ms": max(samples),
            "stddev_ms": round(stdev(samples), 2) if len(samples) > 1 else 0,
            "error_pct": round(100 * (count - data["success_count"]) / count, 2) if count else 0.0,
            "throughput_rps": round(count / duration_sec, 2),
            "received_kbps": round(data["total_bytes"] / 1024 / duration_sec, 2),
            "sent_kbps": round(data["sent_bytes"] / 1024 / duration_sec, 2),
            "avg_bytes": round(data["total_bytes"] / count, 2) if count else 0  # bytes
        })

    # TOTAL row calculation
    all_samples = []
    total_received = 0
    total_sent = 0
    total_success = 0
    total_count = 0

    for data in summary.values():
        all_samples.extend(data["samples"])
        total_received += data["total_bytes"]
        total_sent += data["sent_bytes"]
        total_success += data["success_count"]
        total_count += len(data["samples"])

    if total_count > 0:
        duration_sec = sum(all_samples) / 1000
        result.append({
            "label": "TOTAL",
            "samples": total_count,
            "average_ms": round(mean(all_samples), 2),
            "min_ms": min(all_samples),
            "max_ms": max(all_samples),
            "stddev_ms": round(stdev(all_samples), 2) if len(all_samples) > 1 else 0,
            "error_pct": round(100 * (total_count - total_success) / total_count, 2),
            "throughput_rps": round(total_count / duration_sec, 2),
            "received_kbps": round(total_received / 1024 / duration_sec, 2),
            "sent_kbps": round(total_sent / 1024 / duration_sec, 2),
            "avg_bytes": round(total_received / total_count, 2)
        })

    return result






import re

SENSITIVE_PATTERNS = [
    r"user.dir", r"PWD=", r"/tmp/jmeter", r"FullName:", r"IP:", r"JMeterHome=",
    r"java.version=", r"os.name=", r"hostname=", r"Keystore", r"minimal.properties",
    r"java.vm.name=", r"os.arch=", r"os.version=",
    r"/opt/apache-jmeter-[^ ]+", r"Local host = .*", r"Created user preferences directory"
]

ERROR_KEYWORDS = [
    "error", "exception", "failed", "failure", "unable", "cannot"
]

def sanitize_log_line(line: str) -> str:
    original_line = line  # Keep original in case it's error
    
    # ✅ If it's an error line, skip sensitive filtering
    lower_line = line.lower()
    if any(keyword in lower_line for keyword in ERROR_KEYWORDS):
        # Still do brand replacement & path cleanup for errors, but don't drop them
        return _brand_and_clean(line)

    # 🔒 Remove sensitive lines (non-error lines only)
    for pattern in SENSITIVE_PATTERNS:
        if re.search(pattern, line, re.IGNORECASE):
            return ""

    return _brand_and_clean(line)


def _brand_and_clean(line: str) -> str:
    # Brand replacement
    line = re.sub(r"\borg\.apache\.jmeter\b", "org.neeyatai.kickload", line)
    line = re.sub(r"\bJMeter([A-Z])", r"KickLoad\1", line)
    line = re.sub(r"\bJMeter\b", "KickLoad", line)

    # Replace copyright line
    line = re.sub(r"Copyright \(c\) \d{4}-\d{4} The Apache Software Foundation",
                  "Copyright (c) 2025 NeeyatAI",
                  line)

    # Redact specific paths and dirs
    line = re.sub(r"uploads_[^/]+", "uploads_user", line)
    line = re.sub(r"/tmp/jmeter/[^\s]+", "[temp_path]", line)
    line = re.sub(r"/opt/apache-jmeter-[^\s]+", "[jmeter_path]", line)
    line = re.sub(r"/app", "[app_dir]", line)

    # Generalize timestamps
    line = re.sub(r'@ \d{4} .* UTC', '@ [timestamp]', line)

    # Hide UUIDs, hashes, long IDs
    line = re.sub(r"\b[0-9a-f]{32,}\b", "[uuid]", line)
    line = re.sub(r"\b[0-9a-f]{8,}\b", "[id]", line)

    return line.strip()




def run_jmeter_internal(original_jmx_path, original_result_path, log_channel=None):

    import os, subprocess, logging, time, uuid, shutil
    import sys

    logger = logging.getLogger(__name__)

    JMETER_BIN = "/opt/apache-jmeter-5.6.3/bin/jmeter"

    scratch_dir = None
    try:
        start_time = time.time()

        if not os.path.exists(original_jmx_path):
            raise FileNotFoundError(f".jmx file not found at: {original_jmx_path}")
        logger.info(f"📂 .jmx file exists at: {original_jmx_path}")

        # Use isolated scratch dir only for config files
        user_dir_name = os.path.basename(os.path.dirname(original_jmx_path))
        scratch_id = uuid.uuid4().hex[:8]
        scratch_dir = os.path.join("/tmp/jmeter", f"{user_dir_name}_{scratch_id}")
        os.makedirs(scratch_dir, exist_ok=True)
        logger.info(f"📁 Created scratch dir: {scratch_dir}")

        # Copy .jmx to scratch dir for safety
        jmx_copy_path = os.path.join(scratch_dir, os.path.basename(original_jmx_path))
        shutil.copyfile(original_jmx_path, jmx_copy_path)
        logger.info(f"📋 Copied .jmx to scratch path: {jmx_copy_path}")

        # After copying the .jmx
        for f in os.listdir(os.path.dirname(original_jmx_path)):
            if f.endswith(".csv"):
                src = os.path.join(os.path.dirname(original_jmx_path), f)
                dst = os.path.join(scratch_dir, f)
                shutil.copyfile(src, dst)
                logger.info(f"📋 Copied CSV dependency: {src} → {dst}")


        # Write minimal.properties
        properties_path = os.path.join(scratch_dir, "minimal.properties")
        with open(properties_path, "w") as f:
            f.write(MINIMAL_PROPERTIES_CONTENT.strip())
        logger.info(f"📝 Wrote minimal.properties at: {properties_path}")

        # Run JMeter — result written to original_result_path (not deleted)
        cmd = [
            JMETER_BIN,
            "-n",
            "-t", jmx_copy_path,
            "-l", original_result_path,
            "-q", properties_path
        ]

        logger.info(f"🚀 Running JMeter: {' '.join(cmd)}")

        timeout = int(os.getenv("JMETER_TIMEOUT", 300))
        env = os.environ.copy()
        env["JVM_ARGS"] = "-Dlog4j.configurationFile=/opt/apache-jmeter-5.6.3/config/log4j2-silent.xml"

        with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, env=env) as process:

            full_output = ""
            try:
                for line in iter(process.stdout.readline, ''):
                    full_output += line
                    if log_channel:
                        safe_line = sanitize_log_line(line.strip())
                        if safe_line:  # empty means it was filtered out
                            redis_client.publish(log_channel, safe_line)

                    sys.stdout.flush()  # Ensure immediate stdout flush (optional for debugging)

            except Exception as e:
                logger.warning(f"⚠️ Error while reading subprocess output: {e}")

            # 🧹 Drain remaining output (in case JMeter stops suddenly and line is half-written)
            leftover = process.stdout.read()
            if leftover:
                full_output += leftover
                if log_channel:
                    redis_client.publish(log_channel, leftover.strip())

            retcode = process.wait()
            if retcode != 0:
                raise RuntimeError("JMeter exited with non-zero status.")



        if not os.path.exists(original_result_path):
            raise RuntimeError(f"Expected .jtl result file not found at: {original_result_path}")

        duration = time.time() - start_time
        logger.info(f"⏱️ JMeter test completed in {duration:.2f}s")

        return parse_jtl_summary(original_result_path)



    except subprocess.TimeoutExpired as e:
        logger.warning(f"⚠️ JMeter timed out after {timeout}s: {e}")
        if os.path.exists(original_result_path):
            logger.info("Partial .jtl file found and will be used.")
        else:
            logger.error("No .jtl file found after timeout.")
        raise RuntimeError(f"JMeter timed out after {timeout} seconds.") from e


    finally:
        if scratch_dir and os.path.exists(scratch_dir):
            shutil.rmtree(scratch_dir, ignore_errors=True)
            logger.info(f"🧹 Deleted scratch directory: {scratch_dir}")

