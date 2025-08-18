from celery import Celery
from celery.schedules import crontab
import os
from dotenv import load_dotenv

load_dotenv()

# Use environment vars or fallback to defaults
BROKER_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", BROKER_URL)  # fallback to same Redis

celery = Celery(
    "tasks",
    broker=BROKER_URL,
    backend=RESULT_BACKEND,
    include=["tasks.tasks"]
)

celery.conf.update(
    task_serializer='json',
    result_serializer='json',
    accept_content=['json'],
    timezone='UTC',
    task_track_started=True,  # Optional: allows STARTED state
    result_expires=3600,      # Optional: expire results after 1 hour
    beat_schedule={
        'check-expired-users-every-hour': {
            'task': 'tasks.tasks.check_expiry_task',
            'schedule': crontab(minute=0, hour='*'),  # Every hour
            'options': {'queue': 'scheduler'},
        },
        'cleanup-old-trial-users-every-5-days': {
            'task': 'tasks.tasks.cleanup_expired_trials_task',
            'schedule': crontab(minute=0, hour=0, day_of_month='*/5'),  # ✅ every 5 days at midnight UTC
            'options': {'queue': 'scheduler'},
        }
    }
)

# For compatibility with @shared_task
shared_task = celery.task

# Routing for queues
celery.conf.task_routes = {
    'tasks.tasks.run_jmeter_test_async': {'queue': 'jmeter'},
    'tasks.tasks.generate_gemini_analysis_async': {'queue': 'gemini'},
    'tasks.tasks.check_expiry_task': {'queue': 'scheduler'},
    'tasks.tasks.cleanup_expired_trials_task': {'queue': 'scheduler'},
}

# Update with additional routing
celery.conf.task_routes.update({
    'tasks.tasks.validate_jmx_task': {'queue': 'jmeter'},
})

celery.conf.task_default_queue = 'celery'

