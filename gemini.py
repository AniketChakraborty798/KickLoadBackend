import os
import google.api_core.exceptions
from google import genai

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=GEMINI_API_KEY)

def generate_with_gemini(prompt):
    try:
        # Allow model override via env var and provide fallbacks for availability changes.
        # Google occasionally retires model IDs for new users; this keeps the backend working.
        primary_model = os.getenv("GEMINI_MODEL")
        candidate_models = [m for m in [
            primary_model,
            "gemini-2.5-flash",
            "gemini-2.0-flash-lite",
            "gemini-1.5-flash",
        ] if m]

        last_err = None
        for model in candidate_models:
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=prompt
                )
                return response.text.strip() if hasattr(response, "text") else ""
            except google.api_core.exceptions.GoogleAPIError as api_error:
                # Retry on model availability issues (404/NOT_FOUND), otherwise propagate.
                msg = str(api_error)
                last_err = api_error
                if "NOT_FOUND" in msg or "no longer available" in msg or "model" in msg and "not" in msg and "available" in msg:
                    continue
                raise

        raise RuntimeError(
            f"KickLoad model selection failed. Tried models: {candidate_models}. Last error: {last_err}"
        )
    except google.api_core.exceptions.DeadlineExceeded:
        raise RuntimeError("KickLoad request timed out.")
    except google.api_core.exceptions.GoogleAPIError as api_error:
        raise RuntimeError(f"KickLoad API error: {str(api_error)}")
    except Exception as e:
        raise RuntimeError(f"Unexpected KickLoad error: {str(e)}")


