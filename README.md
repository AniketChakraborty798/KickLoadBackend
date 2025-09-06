# Backend Documentation

This backend powers the **Neeyat AI Platform**, handling authentication,
scaling JMeter for load tests, payments, task management, user
management, and integrations with external services like Jenkins,
GitHub, Razorpay, and Cloudflare.

------------------------------------------------------------------------

## 📂 Project Structure

``` bash
.
├── asgutils/
│   └── asg.py                # Auto-scaling JMeter to handle up to 1M virtual users
│
├── auth/
│   └── decorator.py          # JWT + API token decorators for route protection
│
├── certs/                    # Cloudflare certificates for domains
│   ├── api.neeyatai.com
│   └── jenkins.neeyatai.com
│
├── jenkins/
│   ├── github_integration.py # GitHub integration logic
│   └── jenkins_routes.py     # Jenkins API routes
│
├── payments/
│   ├── razor.py              # Razorpay payment integration
│   └── routes.py             # Payment routes
│
├── tasks/
│   ├── celery.py             # Celery app configuration
│   └── tasks.py              # Task definitions (async jobs)
│
├── users/
│   ├── __init__.py           # JWT auth manager & default configs
│   ├── auth.py               # Login, signup, logout, session handling
│   ├── license_utils.py      # License details for users
│   ├── models.py             # MongoDB configurations
│   ├── schedular.py          # Email triggers for trial/paid expiry
│   └── utils.py              # AWS S3 configurations
│
├── utils/
│   └── pdf_generator.py      # Generates JTL performance test PDFs
│
├── app.py                    # Flask app + main routes
├── clean_and_up.sh           # Full reset script for backend
├── compare_utils.py          # Compare JTL test results
├── email_utils.py            # Email configurations (SMTP, templates, etc.)
├── extract_params_from_jmx.py# Extracts parameters from JMX files
├── gemini.py                 # Gemini API connection
├── generate_test_plan.py     # Generates JMX test plans
├── intelligenet_test_analysis.py # JTL analysis logic
├── jmeter_core.py            # JMeter core connection & runner
├── quick-clean.sh            # Lightweight Docker cleanup/restart
├── requirements.txt          # Python dependencies
```

------------------------------------------------------------------------

## 🛠️ Useful Scripts

-   **`clean_and_up.sh`** → Full reset of backend environment\
-   **`quick-clean.sh`** → Lightweight Docker cleanup & restart

------------------------------------------------------------------------

## 📌 Features Overview

-   **JMeter Scaling** → Run distributed tests with up to 1M virtual
    users.\
-   **Authentication** → JWT + API Token decorators for flexible
    security.\
-   **Payments** → Razorpay integration for handling subscriptions.\
-   **Tasks** → Celery workers for async processing.\
-   **User Management** → Login/signup, license handling, email
    scheduling.\
-   **Integrations** → Jenkins + GitHub + Gemini APIs.\
-   **Reports** → PDF generation & intelligent JTL test analysis.

------------------------------------------------------------------------

## 👨‍💻 Developer Notes

-   `compare_utils.py` to compare JTL results from different runs.\
-   `generate_test_plan.py` to auto-generate JMX files for tests.\
-   `intelligent_test_analysis.py` to analyze the jtl file.\
-   Certificates in `certs/` are **sensitive** → never commit them to
    Git.

------------------------------------------------------------------------

## 📜 License

This project is proprietary and maintained by **Neeyat AI**.
