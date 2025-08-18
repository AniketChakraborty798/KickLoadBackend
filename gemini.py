import os
import google.api_core.exceptions
from google import genai

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=GEMINI_API_KEY)

def generate_with_gemini(prompt):
    try:
        print("Gemini prompt:", prompt[:300])  # Truncated for logs
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt
        )
        return response.text.strip() if hasattr(response, "text") else ""
    except google.api_core.exceptions.DeadlineExceeded:
        raise RuntimeError("Gemini request timed out.")
    except google.api_core.exceptions.GoogleAPIError as api_error:
        raise RuntimeError(f"Gemini API error: {str(api_error)}")
    except Exception as e:
        raise RuntimeError(f"Unexpected Gemini error: {str(e)}")


