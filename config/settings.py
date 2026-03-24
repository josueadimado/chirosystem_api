import os
from datetime import timedelta
from pathlib import Path
import dj_database_url
from celery.schedules import crontab

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "django-insecure-dev-change-in-production")
DEBUG = os.environ.get("DEBUG", "true").lower() in ("true", "1", "yes")

ALLOWED_HOSTS = [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]
CSRF_TRUSTED_ORIGINS = [
    o.strip()
    for o in os.environ.get(
        "CSRF_TRUSTED_ORIGINS",
        "http://localhost:8000,http://127.0.0.1:8000,http://localhost:3000,http://127.0.0.1:3000",
    ).split(",")
    if o.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "corsheaders",
    "rest_framework",
    "drf_spectacular",
    "apps.accounts",
    "apps.clinic",
    "apps.notifications",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# ---------------------------------------------------------
# Database
# ---------------------------------------------------------
DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    DATABASE_URL = (
        f"postgresql://{os.environ.get('POSTGRES_USER', 'postgres')}:"
        f"{os.environ.get('POSTGRES_PASSWORD', 'postgres')}@"
        f"{os.environ.get('POSTGRES_HOST', 'localhost')}:"
        f"{os.environ.get('POSTGRES_PORT', '5432')}/"
        f"{os.environ.get('POSTGRES_DB', 'chiroflow')}"
    )

DATABASES = {
    "default": dj_database_url.config(
        default=DATABASE_URL,
        conn_max_age=30,
        conn_health_checks=True,
    )
}

AUTH_USER_MODEL = "accounts.User"

REST_FRAMEWORK = {
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
}

SPECTACULAR_SETTINGS = {
    "TITLE": "ChiroFlow API",
    "DESCRIPTION": "API-first chiropractic clinic platform",
    "VERSION": "1.0.0",
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=30),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

CORS_ALLOW_ALL_ORIGINS = True

CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = CELERY_BROKER_URL
# Used for “tomorrow” appointment SMS reminders and Celery Beat crontab
CLINIC_TIMEZONE = os.getenv("CLINIC_TIMEZONE", "America/Detroit")
CELERY_TIMEZONE = CLINIC_TIMEZONE
CELERY_BEAT_SCHEDULE = {
    "send-daily-appointment-sms-reminders": {
        "task": "apps.notifications.tasks.send_daily_appointment_reminders",
        "schedule": crontab(hour=9, minute=0),
    },
}

# Twilio SMS (optional). If any of these are missing, SMS is skipped — booking still works.
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "")
# Voice webhooks: public https base as Twilio sees it (no trailing slash), e.g. https://api.yourclinic.com
TWILIO_VOICE_PUBLIC_BASE_URL = os.getenv("TWILIO_VOICE_PUBLIC_BASE_URL", "").strip().rstrip("/")
# Dev only: skip Twilio X-Twilio-Signature check (never enable in production).
VOICE_SKIP_TWILIO_SIGNATURE = os.getenv("VOICE_SKIP_TWILIO_SIGNATURE", "").strip() in ("1", "true", "yes")

# OpenAI — optional; enables phone speech → structured booking on Twilio voice webhooks.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_VOICE_MODEL = os.getenv("OPENAI_VOICE_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"

# Google Calendar — personal account per doctor (OAuth). Callback URL must match Google Cloud Console.
GOOGLE_OAUTH_CLIENT_ID = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "")
# e.g. http://localhost:8001/api/v1/doctor/google_calendar/oauth/callback/ (Docker dev default host port)
GOOGLE_OAUTH_REDIRECT_URI = os.getenv("GOOGLE_OAUTH_REDIRECT_URI", "").strip()

# Square (optional — Web Payments, Terminal, payment links). https://developer.squareup.com/docs
SQUARE_ACCESS_TOKEN = os.getenv("SQUARE_ACCESS_TOKEN", "")
SQUARE_APPLICATION_ID = os.getenv("SQUARE_APPLICATION_ID", "")
SQUARE_LOCATION_ID = os.getenv("SQUARE_LOCATION_ID", "")
# sandbox | production
SQUARE_ENVIRONMENT = os.getenv("SQUARE_ENVIRONMENT", "sandbox").strip().lower()
# Paired Square Terminal device id (Developer Dashboard → Devices)
SQUARE_DEVICE_ID = os.getenv("SQUARE_DEVICE_ID", "").strip()
# Webhook signature key + exact notification URL string from Developer Console (for HMAC verification)
SQUARE_WEBHOOK_SIGNATURE_KEY = os.getenv("SQUARE_WEBHOOK_SIGNATURE_KEY", "").strip()
SQUARE_WEBHOOK_NOTIFICATION_URL = os.getenv("SQUARE_WEBHOOK_NOTIFICATION_URL", "").strip()
# Used for Square payment link redirect (no trailing slash)
FRONTEND_BASE_URL = os.getenv("FRONTEND_BASE_URL", "http://localhost:3001")
# If Square adds cancel_url to CheckoutOptions, set to true to send it (default off — avoids API errors)
SQUARE_CHECKOUT_SEND_CANCEL_URL = os.getenv("SQUARE_CHECKOUT_SEND_CANCEL_URL", "").strip().lower() in (
    "1",
    "true",
    "yes",
)


# ---------------------------------------------------------
# CORS
# ---------------------------------------------------------
CORS_ALLOW_ALL_ORIGINS = DEBUG
CORS_ALLOWED_ORIGINS = [
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
] + ([o for o in os.environ.get("CORS_ALLOWED_ORIGINS", "").split(",") if o] if not DEBUG else [])
