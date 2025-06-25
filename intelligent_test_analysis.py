import pandas as pd
import json
import os
import logging
import xml.etree.ElementTree as ET
from tasks.tasks import generate_gemini_analysis_async
from datetime import datetime

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('jmeter.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

def parse_xml_jtl(file_path):
    """
    Parse XML JTL file and return a DataFrame with required columns.
    """
    try:
        tree = ET.parse(file_path)
        root = tree.getroot()

        rows = []
        for sample in root.findall(".//httpSample"):
            label = sample.attrib.get("lb")
            elapsed = sample.attrib.get("t")
            response_code = sample.attrib.get("rc")
            all_threads = sample.attrib.get("allThreads") or sample.attrib.get("ng")  # fallback to ng

            try:
                elapsed = int(elapsed)
                all_threads = int(all_threads) if all_threads is not None else 1
            except Exception:
                elapsed = None
                all_threads = 1

            rows.append({
                "label": label,
                "elapsed": elapsed,
                "responseCode": response_code,
                "allThreads": all_threads
            })

        df = pd.DataFrame(rows)
        df.dropna(subset=["label", "elapsed", "responseCode"], inplace=True)
        return df
    except Exception as e:
        logger.error(f"Failed to parse XML JTL file: {e}")
        return None

def analyze_jtl(file_path, output_folder):
    try:
        logger.info(f"📊 Starting analysis for {file_path}")

        # Detect if file is XML or CSV
        if file_path.lower().endswith(".xml") or file_path.lower().endswith(".jtl"):
            df = parse_xml_jtl(file_path)
            if df is None:
                try:
                    df = pd.read_csv(file_path)
                except Exception as e:
                    logger.error(f"❌ Failed to read JTL file as CSV: {e}")
                    return {"error": "Invalid JTL file format or unreadable."}
        else:
            try:
                df = pd.read_csv(file_path)
            except Exception as e:
                logger.error(f"❌ Failed to read JTL file as CSV: {e}")
                return {"error": "Invalid JTL file format or unreadable."}

        required_columns = {"label", "elapsed", "responseCode", "allThreads"}
        if not required_columns.issubset(df.columns):
            missing = required_columns - set(df.columns)
            return {"error": f"Missing required columns: {missing}"}

        # Ensure we have enough rows to analyze meaningfully
        if df.shape[0] < 2:
            return {"error": "Insufficient test data for meaningful analysis."}

        summary = df.groupby("label").agg(
            avg_response_time=("elapsed", "mean"),
            error_rate=("responseCode", lambda x: (x.astype(str) != "200").mean() * 100),
            throughput=("label", "count"),
            concurrent_users=("allThreads", "max")
        ).reset_index()

        if summary.empty:
            return {"error": "No valid data found in JTL."}

        summary = summary.round(2)

        summary_markdown = "\n".join(
            f"- **{row['label']}**: Avg Time = `{row['avg_response_time']}ms`, "
            f"Errors = `{row['error_rate']}%`, "
            f"Throughput = `{row['throughput']}`, Users = `{row['concurrent_users']}`"
            for _, row in summary.iterrows()
        )

        prompt = (
            "You are an expert in analyzing Apache JMeter performance test results.\n"
            "Below is a summary of key metrics from a recent test, grouped by HTTP endpoint label:\n\n"
            f"{summary_markdown}\n\n"
            "Analyze the test results and provide a detailed markdown summary that includes:\n"
            "- Whether performance is acceptable for each label\n"
            "- Identify any bottlenecks, high response times, or error rates\n"
            "- Suggestions for improvement or next steps\n\n"
            "Do NOT include code blocks or unrelated explanations. Just return the markdown analysis."
        )

        try:
            task = generate_gemini_analysis_async.delay(prompt)
            raw_result = task.get(timeout=60).strip()
        except Exception as e:
            logger.error(f"❌ Gemini timeout or error: {e}")
            return {"error": f"Gemini generation failed: {str(e)}"}

        # Handle JSON response or raw markdown
        try:
            parsed = json.loads(raw_result)
            markdown_text = parsed.get("analysis", raw_result)
        except Exception:
            markdown_text = raw_result

        # Remove markdown fences more robustly
        for fence in ["```markdown", "```md", "```"]:
            if markdown_text.startswith(fence):
                markdown_text = markdown_text[len(fence):].strip()
            if markdown_text.endswith("```"):
                markdown_text = markdown_text[:-3].strip()

        # Fallback check for empty/hallucinated response
        if not markdown_text or "Error:" in markdown_text:
            return {"error": "Gemini response could not be parsed or was empty."}

        os.makedirs(output_folder, exist_ok=True)
        timestamp = datetime.now().strftime("%d-%m-%Y_%H-%M-%S")
        filename = f"analysis_{timestamp}.md"
        output_path = os.path.join(output_folder, filename)

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(markdown_text)

        logger.info(f"✅ Analysis saved: {output_path}")

        return {
            "analysis": markdown_text,
            "filename": filename
        }

    except Exception as e:
        logger.error(f"❌ Unexpected analysis error: {e}")
        return {"error": f"Unexpected error: {str(e)}"}

