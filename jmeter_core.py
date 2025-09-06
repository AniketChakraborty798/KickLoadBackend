import os
import subprocess
import logging
import time
import uuid
import shutil
import redis 
import openpyxl
import csv

logger = logging.getLogger(__name__)


redis_client = redis.StrictRedis(host="redis", port=6379, password=os.getenv("REDIS_PASSWORD"), decode_responses=True)

JMETER_BIN = "/opt/apache-jmeter-5.6.3/bin/jmeter"



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
    # Set or create stringProp, intProp, or longProp with a non-empty value
    for tag in ["stringProp", "intProp", "longProp"]:
        node = elem.find(f".//{tag}[@name='{name}']")
        if node is not None:
            val = str(value).strip() if value is not None else ""
            # If empty numeric property, set "0"
            if tag in ["intProp", "longProp"] and (val == "" or val is None):
                val = "0"
            # For stringProp, also default to empty string safely
            node.text = val
            return True
    # If not found, create a longProp (default numeric) if numeric value, else stringProp
    val = str(value).strip() if value is not None else ""
    if val == "" or val is None:
        val = "0"
    # Decide tag type for creation
    tag_to_create = "stringProp"
    if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
        tag_to_create = "longProp"
    new_node = ET.SubElement(elem, tag_to_create, name=name)
    new_node.text = val
    return True

def set_bool_prop(elem, name, value):
    node = elem.find(f".//boolProp[@name='{name}']")
    text_val = "true" if value else "false"
    if node is not None:
        node.text = text_val
        return True
    # If boolProp not found, create it
    new_node = ET.SubElement(elem, "boolProp", name=name)
    new_node.text = text_val
    return True

def apply_overrides_to_jmx(original_path, output_path, overrides):
    import xml.etree.ElementTree as ET
    tree = ET.parse(original_path)
    root = tree.getroot()

    # Normalize sampler_error_action to valid JMeter values
    valid_actions = {
        "continue": "continue",
        "startnextloop": "startnextloop",
        "stopthread": "stopthread",
        "stoptest": "stoptest",
        "stoptestnow": "stoptestnow"
    }

    for tg in root.iter("ThreadGroup"):
        # num_threads
        if "num_threads" in overrides:
            val = overrides.get("num_threads")
            if val in [None, "", "NA"]:
                val = "0"
            set_prop(tg, "ThreadGroup.num_threads", val)

        # ramp_time
        if "ramp_time" in overrides:
            val = overrides.get("ramp_time")
            if val in [None, "", "NA"]:
                val = "0"
            set_prop(tg, "ThreadGroup.ramp_time", val)

        # Find main loop controller
        main_controller = next((elem for elem in tg.findall("elementProp") if elem.attrib.get("name") == "ThreadGroup.main_controller"), None)
        if main_controller is not None:
            # loop_count
            if "loop_count" in overrides:
                val = overrides.get("loop_count")
                if val in [None, "", "NA"]:
                    val = "0"
                set_prop(main_controller, "LoopController.loops", val)
            # always disable continue_forever
            set_bool_prop(main_controller, "LoopController.continue_forever", False)

        # scheduler (ThreadGroup.scheduler) - ensure boolean
        if "specify_thread_lifetime" in overrides:
            val = bool(overrides.get("specify_thread_lifetime"))
            set_bool_prop(tg, "ThreadGroup.scheduler", val)

        # duration - numeric default to 0 if empty
        if "duration" in overrides:
            val = overrides.get("duration")
            if val in [None, "", "NA"]:
                val = "0"
            set_prop(tg, "ThreadGroup.duration", val)

        # startup_delay aka ThreadGroup.delay - numeric default to 0 if empty or missing
        if "startup_delay" in overrides:
            val = overrides.get("startup_delay")
            if val in [None, "", "NA"]:
                val = "0"
            set_prop(tg, "ThreadGroup.delay", val)

        # Sampler error action, normalize to a valid value, default to 'continue' if invalid
        if "sampler_error_action" in overrides:
            val = overrides.get("sampler_error_action", "").lower().replace("_","")
            val = valid_actions.get(val, "continue")
            set_prop(tg, "ThreadGroup.on_sample_error", val)

        # same_user_on_next_iteration - boolean
        if "same_user_on_iteration" in overrides:
            val = bool(overrides.get("same_user_on_iteration"))
            set_bool_prop(tg, "ThreadGroup.same_user_on_next_iteration", val)

        # delay_thread_creation aka ThreadGroup.delayedStart - boolean
        if "delay_thread_creation" in overrides:
            val = bool(overrides.get("delay_thread_creation"))
            set_bool_prop(tg, "ThreadGroup.delayedStart", val)

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
import re


def parse_jtl_summary(jtl_path):
    """
    Parse JTL results and produce a summary aligned with JMeter Summary Report:
    - Only counts parent samples (no subsamples/redirects)
    - Throughput is calculated based on wall-clock test duration
    - Byte counts averaged correctly
    """
    tree = ET.parse(jtl_path)
    root = tree.getroot()

    summary = defaultdict(lambda: {
        "samples": [],
        "total_bytes": 0,
        "sent_bytes": 0,
        "success_count": 0
    })

    all_timestamps = []

    def normalize_label(label: str) -> str:
        # Remove things like "HTTP Request-0" → "HTTP Request"
        return re.sub(r'-\d+$', '', label)

    def add_sample_data(node):
        """Only add top-level samples matching JMeter Summary Report (no recursion)."""
        label = node.get("lb") or "Unknown"
        label = normalize_label(label)

        elapsed = int(node.get("t", 0))
        success = node.get("s") == "true"
        received = int(node.get("by", 0))
        sent = int(node.get("sby", 0))
        timestamp = int(node.get("ts", 0))

        summary[label]["samples"].append(elapsed)
        summary[label]["total_bytes"] += received
        summary[label]["sent_bytes"] += sent
        if success:
            summary[label]["success_count"] += 1

        if timestamp > 0:
            all_timestamps.append(timestamp)

        # ❌ Do NOT recurse into child <httpSample> or <sample>.
        # JMeter GUI Summary doesn't double-count subsamples.

    # Process only root-level samples
    for sample_node in root:
        if 'sample' in sample_node.tag.lower():
            add_sample_data(sample_node)

    if not summary:
        return [{
            "label": "NO SAMPLES CAPTURED",
            "samples": 0,
            "average_ms": 0,
            "min_ms": 0,
            "max_ms": 0,
            "stddev_ms": 0,
            "error_pct": 0,
            "throughput_rps": 0,
            "received_kbps": 0,
            "sent_kbps": 0,
            "avg_bytes": 0
        }]

    # Duration in seconds from earliest to latest timestamp
    if all_timestamps:
        min_ts, max_ts = min(all_timestamps), max(all_timestamps)
        duration_sec = max((max_ts - min_ts) / 1000.0, 1)
    else:
        duration_sec = 1

    result = []

    for label, data in summary.items():
        samples = data["samples"]
        count = len(samples)

        result.append({
            "label": label,
            "samples": count,
            "average_ms": round(mean(samples), 2) if count else 0,
            "min_ms": min(samples) if count else 0,
            "max_ms": max(samples) if count else 0,
            "stddev_ms": round(stdev(samples), 2) if len(samples) > 1 else 0,
            "error_pct": round(100 * (count - data["success_count"]) / count, 2) if count else 0.0,
            "throughput_rps": round(count / duration_sec, 2),
            "received_kbps": round(data["total_bytes"] / 1024 / duration_sec, 2),
            "sent_kbps": round(data["sent_bytes"] / 1024 / duration_sec, 2),
            "avg_bytes": round(data["total_bytes"] / count, 2) if count else 0
        })

    # Totals row
    all_samples = []
    total_received = total_sent = total_success = total_count = 0

    for data in summary.values():
        all_samples.extend(data["samples"])
        total_received += data["total_bytes"]
        total_sent += data["sent_bytes"]
        total_success += data["success_count"]
        total_count += len(data["samples"])

    if total_count > 0:
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





SENSITIVE_PATTERNS = [
    r"user.dir", r"PWD=", r"/tmp/jmeter", r"FullName:", r"IP:", r"JMeterHome=",
    r"java.version=", r"os.name=", r"hostname=", r"Keystore", r"minimal.properties",
    r"java.vm.name=", r"os.arch=", r"os.version=",
    r"/opt/apache-jmeter-[^ ]+", r"Local host = .*", r"Created user preferences directory"
]

ERROR_KEYWORDS = [
    "error", "exception", "failed", "failure", "unable", "cannot"
]

# Regex for IPv4 addresses
IPV4_REGEX = r'\b((25[0-5]|2[0-4][0-9]|1\d\d|[1-9]?\d)(\.|$)){4}\b'
# Regex for IPv6 addresses (simplified, covers common cases)
IPV6_REGEX = r'\b(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}\b'

def sanitize_log_line(line: str) -> str:
    original_line = line  # Keep original in case it's error

    lower_line = line.lower()
    if any(keyword in lower_line for keyword in ERROR_KEYWORDS):
        # Preserve error lines but still apply brand & cleanup replacements & mask IPs
        line = _brand_and_clean(line)
        line = _mask_ips(line)
        return line

    # Remove sensitive lines (non-error lines only)
    for pattern in SENSITIVE_PATTERNS:
        if re.search(pattern, line, re.IGNORECASE):
            return ""

    line = _brand_and_clean(line)
    line = _mask_ips(line)
    return line


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

def _mask_ips(line: str) -> str:
    # Mask IPv4 addresses
    line = re.sub(IPV4_REGEX, "[ip]", line)

    # Mask IPv6 addresses
    line = re.sub(IPV6_REGEX, "[ip]", line)

    return line



def run_jmeter_internal(
    original_jmx_path, original_result_path, log_channel=None,
    task_id=None, num_threads=None, user_email=None
):

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
        env["JVM_ARGS"] = "-Dlog4j.configurationFile=/opt/jmeter/bin/log4j2.xml"


        with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, env=env, preexec_fn=os.setsid ) as process:
            # --- Save PID and VU info in Redis ---
            if task_id:
                redis_client.set(f"jmeter_pid:{task_id}", process.pid, ex=12*3600)
            if task_id and num_threads and user_email:
                import json
                vu_info = json.dumps({"email": user_email, "num_threads": int(num_threads)})
                redis_client.set(f"jmeter_vuinfo:{task_id}", vu_info, ex=12*3600)

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

            retcode = process.wait(timeout=timeout)

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

