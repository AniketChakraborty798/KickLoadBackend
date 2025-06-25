import bcrypt
from flask import current_app
from itsdangerous import URLSafeTimedSerializer
import os
from dotenv import load_dotenv
import secrets
from email_utils import send_email
from email_utils import send_email, styled_email_template
import boto3
import os
from botocore.exceptions import ClientError

load_dotenv()

s3 = boto3.client(
    's3',
    aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
    region_name=os.getenv("AWS_REGION")
)

BUCKET_NAME = os.getenv("S3_BUCKET")


def upload_file_to_s3(file_path, s3_key):
    try:
        s3.upload_file(file_path, BUCKET_NAME, s3_key)
        return True
    except ClientError as e:
        print(f"Upload error: {e}")
        return False


def upload_fileobj_to_s3(file_obj, s3_key):
    try:
        s3.upload_fileobj(file_obj, BUCKET_NAME, s3_key)
        return True
    except ClientError as e:
        print(f"Upload error: {e}")
        return False


def download_file_from_s3(s3_key, local_path):
    try:
        s3.download_file(BUCKET_NAME, s3_key, local_path)
        return True
    except ClientError as e:
        print(f"Download error: {e}")
        return False


def generate_presigned_url(s3_key, expiration=3600):
    try:
        url = s3.generate_presigned_url(
            'get_object',
            Params={'Bucket': BUCKET_NAME, 'Key': s3_key},
            ExpiresIn=expiration
        )
        return url
    except Exception as e:
        print(f"❌ Error generating presigned URL: {e}")
        return None

BACKEND_URL = os.getenv("BACKEND_URL")

SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT"))
EMAIL_USER = os.getenv("EMAIL_USER") 
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD") 

# ------------------ Password Utilities ------------------ #
def hash_password(password):
    hashed = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
    return hashed.decode('utf-8')  # Store as string in DB

def check_password(password, hashed):
    if isinstance(hashed, str):
        hashed = hashed.encode('utf-8')  # Convert back to bytes for bcrypt
    return bcrypt.checkpw(password.encode('utf-8'), hashed)
# ------------------ Token Generators ------------------ #

def generate_verification_token(email):
    s = URLSafeTimedSerializer(current_app.config['JWT_SECRET_KEY'])
    return s.dumps(email)

def verify_token(token, max_age=300):
    s = URLSafeTimedSerializer(current_app.config['JWT_SECRET_KEY'])
    try:
        return s.loads(token, max_age=max_age)
    except Exception:
        return None

# ------------------ Email Sender ------------------ #
def send_verification_email(to_email: str, token: str) -> dict:
    link = f"{BACKEND_URL}/verify/{token}"

    subject = "Verify Your Email - JMeterAI Tool"

    message = f"""
    Thank you for signing up!<br><br>
    Please click the button below to verify your email address:
    <p style="text-align: center; margin: 30px 0;">
        <a href="{link}" target="_blank"
           style="background-color: #007bff; color: #fff; padding: 12px 24px; text-decoration: none; border-radius: 5px; font-weight: bold;">
            Verify Email
        </a>
    </p>
    This link is valid for 5 minutes for your security.<br><br>
    If you didn’t sign up for the JMeterAI Tool, you can ignore this email.
    """

    body = styled_email_template("Welcome to JMeterAI Tool", message)

    return send_email(to=to_email, subject=subject, body=body, is_html=True)



def generate_otp():
    return str(secrets.randbelow(900000) + 100000)

def send_otp_email(to_email: str, otp_code: str) -> dict:

    subject = "Your OTP for Password Reset - JMeterAI Tool"

    message = f"""
    We received a request to reset your password for the JMeterAI Tool account.<br><br>
    Your One-Time Password (OTP) is:
    <div style="text-align: center; margin: 30px 0;">
        <span style="display: inline-block; font-size: 28px; letter-spacing: 4px; background-color: #e9f2ff; padding: 12px 24px; border-radius: 5px; color: #007bff; font-weight: bold;">
            {otp_code}
        </span>
    </div>
    This code will expire in 5 minutes and can be used only once.<br><br>
    If you did not request a password reset, please ignore this email.
    """

    body = styled_email_template("Password Reset Request", message)
    return send_email(to=to_email, subject=subject, body=body, is_html=True)
