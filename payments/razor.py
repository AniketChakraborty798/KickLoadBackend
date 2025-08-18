import razorpay
import os
import hmac
import hashlib
import logging

# Set up logger
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)  # You can change to DEBUG for more detail

RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")

# ✅ Define this as a shared client to be imported anywhere
razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

def create_order(amount, receipt_id, currency="INR"):
    try:
        return razorpay_client.order.create({
            "amount": amount,
            "currency": currency,
            "receipt": receipt_id,
            "payment_capture": 1
        })
    except Exception as e:
        logger.error(f"Error creating Razorpay order: {e}")
        return {"error": str(e)}

def verify_signature(order_id, payment_id, signature):
    payload = f"{order_id}|{payment_id}"
    generated_signature = hmac.new(
        RAZORPAY_KEY_SECRET.encode(),
        payload.encode(),
        hashlib.sha256
    ).hexdigest()

    is_valid = generated_signature == signature

    # ✅ Log full verification process (don't log secrets!)
    logger.info(f"Verifying signature:")
    logger.info(f" - order_id: {order_id}")
    logger.info(f" - payment_id: {payment_id}")
    logger.info(f" - payload: {payload}")
    logger.info(f" - received_signature: {signature}")
    logger.info(f" - generated_signature: {generated_signature}")
    logger.info(f" - is_valid: {is_valid}")

    return is_valid

