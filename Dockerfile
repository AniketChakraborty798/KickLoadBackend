FROM python:3.10-slim

# Install system dependencies only (no Java or JMeter)
RUN apt-get update && \
    apt-get install -y curl unzip ca-certificates && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000

CMD ["gunicorn", "-k", "gevent", "--workers=1", "--timeout=120", "-b", "0.0.0.0:5000", "app:app"]

