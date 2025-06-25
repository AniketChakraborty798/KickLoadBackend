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
        return response.text.strip() if hasattr(response, "text") else "❌ No text in response."
    except google.api_core.exceptions.DeadlineExceeded:
        return "❌ Gemini request timed out."
    except google.api_core.exceptions.GoogleAPIError as api_error:
        return f"❌ Gemini API error: {str(api_error)}"
    except Exception as e:
        return f"❌ Unexpected error: {str(e)}"

