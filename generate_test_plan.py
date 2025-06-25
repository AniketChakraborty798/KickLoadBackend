import io
from datetime import datetime
import xml.etree.ElementTree as ET
from users.utils import upload_fileobj_to_s3
import traceback
from tasks.tasks import generate_gemini_analysis_async

def is_valid_jmx(xml_content: str) -> bool:
    try:
        root = ET.fromstring(xml_content)
        if root.tag != "jmeterTestPlan":
            return False

        # Basic structural check
        thread_groups = root.findall(".//ThreadGroup")
        samplers = root.findall(".//HTTPSamplerProxy")
        if not thread_groups or not samplers:
            return False

        # Plugin class detection
        known_classes = {
            "HTTPSamplerProxy", "ThreadGroup", "HeaderManager", "TestPlan",
            "ResponseAssertion", "LoopController", "HashTree"
        }
        for elem in root.iter():
            tag = elem.tag.split('.')[-1]  # ignore full namespace if any
            if tag not in known_classes and "TestPlan" not in tag:
                if "Header" in tag and tag != "HeaderManager":
                    return False
                if tag.startswith("kg.apc") or tag.startswith("jp.") or tag.startswith("com.") or "JSON" in tag:
                    return False

        return True
    except Exception:
        return False



def extract_xml_from_markdown(jmx_response: str) -> str:
    start = jmx_response.find("```xml")
    end = jmx_response.find("```", start + 6)
    if start != -1 and end != -1:
        return jmx_response[start + 6:end].strip()
    return jmx_response.strip()

def generate_jmeter_test_plan(prompt, user_email, max_attempts=10):
    try:
        # Clean and prepare prompt
        user_input = prompt.strip()
        if not user_input or len(user_input.split()) < 3:
            return {
                "status": "error",
                "message": "Please provide a valid description of the JMeter test plan you want to generate."
            }, 400

        # Stronger instruction to AI model
        full_prompt = (
            "You are an expert in Apache JMeter.\n"
            "Generate a valid `.jmx` test plan in XML (JMeter 5.6.3), wrapped inside ```xml ... ```.\n\n"
            f"User Input:\n\"{user_input}\"\n\n"
            "Rules:\n"
            "1. If input lacks a URL, user count, or HTTP method (GET/POST), return:\n"
            "   Error: Insufficient input to generate a JMeter test plan.\n"
            "2. Otherwise, generate a `.jmx` with:\n"
            "- One <TestPlan> root\n"
            "- One <ThreadGroup> for the specified users\n"
            "- One <HTTPSamplerProxy> with the given method/URL\n"
            "- Built-in JMeter components only (no plugins)\n"
            "- Properly nested <hashTree> elements\n"
            "- No extra text — only raw XML in the code block\n"
        )


        for attempt in range(max_attempts):
            task = generate_gemini_analysis_async.delay(full_prompt)
            try:
                raw_response = task.get(timeout=60)
            except Exception as e:
                return {"status": "error", "message": f"Gemini timeout or task failure: {str(e)}"}, 500

            if "Error: Insufficient input" in raw_response:
                return {
                    "status": "error",
                    "message": "The input provided is not specific enough to generate a JMeter test plan."
                }, 400

            xml_only = extract_xml_from_markdown(raw_response)

            if is_valid_jmx(xml_only):
                timestamp = datetime.now().strftime("%d-%m-%Y_%H-%M-%S")
                jmx_filename = f"test_plan_{timestamp}.jmx"
                s3_key = f"uploads/{user_email}/{jmx_filename}"
                file_obj = io.BytesIO(xml_only.encode('utf-8'))
                upload_fileobj_to_s3(file_obj, s3_key)

                return {
                    "status": "success",
                    "message": "Test plan generated and uploaded to S3.",
                    "jmx_filename": jmx_filename
                }, 200

        return {
            "status": "error",
            "message": f"Max retry limit of {max_attempts} reached. No valid JMX plan generated."
        }, 500

    except Exception as e:
        print("❌ Error generating test plan:", traceback.format_exc())
        return {"status": "error", "message": str(e)}, 500

