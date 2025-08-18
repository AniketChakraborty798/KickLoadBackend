from datetime import datetime

def get_license_info(user):
    now = datetime.utcnow()
    trial_ends_at = user.get("trial_ends_at")
    paid_ends_at = user.get("paid_ends_at")

    if paid_ends_at and paid_ends_at > now:
        license_type = "paid"
    elif trial_ends_at and trial_ends_at > now:
        license_type = "trial"
    else:
        license_type = "expired"

    return {
        "license": license_type,
        "trial_ends_at": trial_ends_at.isoformat() + 'Z' if trial_ends_at else None,
        "paid_ends_at": paid_ends_at.isoformat() + 'Z' if paid_ends_at else None,
    }
