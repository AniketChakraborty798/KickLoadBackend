FROM python:3.10-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 👇 Add this line to ensure /app is on PYTHONPATH for Flask, Celery, etc.
ENV PYTHONPATH="/app:${PYTHONPATH}"

EXPOSE 5000

ENV PYTHONUNBUFFERED=1
CMD ["gunicorn", "-k", "gevent", "--workers=1", "--timeout=300", "-b", "0.0.0.0:5000", "app:app"]



