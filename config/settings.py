import os
from datetime import timedelta
from pathlib import Path
import dj_database_url
from celery.schedules import crontab

BASE_DIR = Path(__file__).resolve().parent.parent
DEBUG = os.environ.get("DEBUG", "true").lower() in ("true", "1", "yes")

if DEBUG:
    SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "django-insecure-dev-only")
else:
    # Production: crash loudly if secret key is missing — no insecure fallback
    SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]

_allowed_raw = (
    os.environ.get("ALLOWED_HOSTS")
    or os.environ.get("DJANGO_ALLOWED_HOSTS")
    or "localhost,127.0.0.1"
)
ALLOWED_HOSTS = [h.strip() for h in _allowed_raw.split(",") if h.strip()]
# Docker health checks and Gunicorn bind to 127.0.0.1 — always allow loopback even when production sets only the public domain.
for _loopback in ("127.0.0.1", "localhost", "[::1]"):
    if _loopback not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(_loopback)
# Next.js in Docker proxies to this API using hostname `api` or `host.docker.internal` — those must be allowed or Django returns 400.
_docker_env = os.environ.get("DOCKER_ENV", "").lower() in ("1", "true", "yes")
if _docker_env or os.environ.get("ALLOWED_HOSTS_ALLOW_DOCKER", "").lower() in ("1", "true", "yes"):
    for _h in ("api", "web", "host.docker.internal"):
        if _h not in ALLOWED_HOSTS:
            ALLOWED_HOSTS.append(_h)

CSRF_TRUSTED_ORIGINS = [
    o.strip()
    for o in os.environ.get(
        "CSRF_TRUSTED_ORIGINS",
        "http://localhost:8000,http://127.0.0.1:8000,http://localhost:3000,http://127.0.0.1:3000,"
        "http://localhost:3001,http://127.0.0.1:3001",
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
    "apps.clinic.middleware.hipaa_audit.HIPAAAuditMiddleware",  # HIPAA PHI audit log
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
# Clinic wall clock fallback when DB timezone is unset. Prefer Admin → Settings (ClinicSettings.timezone).
CLINIC_TIMEZONE = os.getenv("CLINIC_TIMEZONE", "America/Detroit")
TIME_ZONE = CLINIC_TIMEZONE
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
# WhiteNoise warns if this path is missing (common in fresh Docker images before collectstatic).
try:
    STATIC_ROOT.mkdir(parents=True, exist_ok=True)
except OSError:
    pass

# Media files — uploaded patient documents, insurance cards, images, etc.
# In production, point MEDIA_ROOT at a volume and serve via nginx (or switch to S3 with django-storages).
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
try:
    MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
except OSError:
    pass

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# In Docker, use redis://redis:6379/0 (Compose service name). Many .env files set REDIS_URL only.
_REDIS_URL_RAW = os.getenv("REDIS_URL", "").strip()
_CELERY_BROKER_RAW = os.getenv("CELERY_BROKER_URL", "").strip()
CELERY_BROKER_URL = _CELERY_BROKER_RAW or _REDIS_URL_RAW or "redis://localhost:6379/0"
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "").strip() or CELERY_BROKER_URL

# Django cache: login email OTP, webhook dedupe, voice rate limits. LocMem is NOT shared between
# Gunicorn/uWSGI workers — OTP set on worker A is invisible to verify on worker B → “invalid code”.
# When Redis is explicitly configured (env or Docker Compose CELERY_BROKER_URL), use it for cache.
_CACHE_REDIS_URL = os.getenv("CACHE_REDIS_URL", "").strip() or _REDIS_URL_RAW or _CELERY_BROKER_RAW
if _CACHE_REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": _CACHE_REDIS_URL,
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "chiroflow",
        }
    }
# CELERY_TIMEZONE is set from env at worker startup (not read from DB on each task).
# To change clinic timezone for the app, use Admin → Settings (saved on ClinicSettings).
# Restart Celery workers after changing CELERY_TIMEZONE in .env if you use env override.
CELERY_TIMEZONE = CLINIC_TIMEZONE
# Kiosk: earliest patient self-service check-in on the appointment day (minutes before start). Staff desk check-in can be earlier.
KIOSK_EARLY_CHECKIN_MINUTES_BEFORE = int(os.getenv("KIOSK_EARLY_CHECKIN_MINUTES_BEFORE", "30"))
CELERY_BEAT_SCHEDULE = {
    "send-daily-appointment-sms-reminders": {
        "task": "apps.notifications.tasks.send_daily_appointment_reminders",
        "schedule": crontab(hour=9, minute=0),
    },
    "process-auto-no-show-appointments": {
        "task": "apps.notifications.tasks.process_auto_no_show_appointments_task",
        "schedule": crontab(minute="*/15"),
    },
}

# Twilio SMS (optional). If any of these are missing, SMS is skipped — booking still works.
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER", "")
TWILIO_MESSAGING_SERVICE_SID = os.getenv("TWILIO_MESSAGING_SERVICE_SID", "")
# Voice webhooks: public https base as Twilio sees it (no trailing slash), e.g. https://api.yourclinic.com
TWILIO_VOICE_PUBLIC_BASE_URL = os.getenv("TWILIO_VOICE_PUBLIC_BASE_URL", "").strip().rstrip("/")
# Dev only: skip Twilio X-Twilio-Signature check (never enable in production).
VOICE_SKIP_TWILIO_SIGNATURE = os.getenv("VOICE_SKIP_TWILIO_SIGNATURE", "").strip() in ("1", "true", "yes")

# ConversationRelay WebSocket server (voice_relay.py). Public wss:// URL that Twilio can reach.
VOICE_WS_PUBLIC_URL = os.getenv("VOICE_WS_PUBLIC_URL", "").strip().rstrip("/")
# ElevenLabs voice ID for ConversationRelay TTS (pick from Twilio's library or use a cloned voice).
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "").strip()
# ConversationRelay TTS: ElevenLabs | Google | Amazon. Use Google if calls drop immediately (Twilio/ElevenLabs setup).
CONVERSATION_RELAY_TTS_PROVIDER = os.getenv("CONVERSATION_RELAY_TTS_PROVIDER", "ElevenLabs").strip() or "ElevenLabs"
# Voice name for that provider (Twilio docs). Google example: en-US-Journey-O. Leave blank for provider defaults.
# If set for ElevenLabs, this is the FULL `voice` string (voice id + optional -model + optional -tuning); other ElevenLabs envs below are ignored.
CONVERSATION_RELAY_TTS_VOICE = os.getenv("CONVERSATION_RELAY_TTS_VOICE", "").strip()
# ElevenLabs (ConversationRelay only): append TTS model to voice id — flash_v2_5 (Twilio default), turbo_v2_5, turbo_v2, flash_v2.
# https://www.twilio.com/docs/voice/conversationrelay/voice-configuration
ELEVENLABS_TTS_MODEL = os.getenv("ELEVENLABS_TTS_MODEL", "").strip()
# After model: speed_stability_similarity (e.g. 1.0_0.6_0.8). Speed 0.7–1.2; stability & similarity 0.0–1.0.
ELEVENLABS_TTS_VOICE_TUNING = os.getenv("ELEVENLABS_TTS_VOICE_TUNING", "").strip()
# Twilio <ConversationRelay> when ttsProvider is ElevenLabs: on | off | auto (auto behaves like off for voice calls).
CONVERSATION_RELAY_ELEVENLABS_TEXT_NORMALIZATION = os.getenv(
    "CONVERSATION_RELAY_ELEVENLABS_TEXT_NORMALIZATION", ""
).strip().lower()

# OpenAI — optional; enables phone speech → structured booking on Twilio voice webhooks.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
# Phone voice (Sarah): gpt-5.4-nano is OpenAI’s fastest/cheapest frontier tier for low latency. If your key
# returns “model not found”, set OPENAI_VOICE_MODEL=gpt-4o-mini (or gpt-5.4-mini) in .env.
OPENAI_VOICE_MODEL = os.getenv("OPENAI_VOICE_MODEL", "gpt-5.4-nano").strip() or "gpt-5.4-nano"
# OpenAI Realtime (gpt-realtime): alloy, ash, ballad, coral, echo, sage, shimmer, verse, marin, cedar
OPENAI_REALTIME_VOICE = os.getenv("OPENAI_REALTIME_VOICE", "coral").strip() or "coral"
# Voice relay: stream LLM tokens to Twilio (lower perceived delay). Set VOICE_LLM_STREAM=false to disable.
VOICE_LLM_STREAM = os.getenv("VOICE_LLM_STREAM", "true").strip().lower() in ("1", "true", "yes")
# Empty transcripts from ConversationRelay (silence / STT hiccup) before we hang up with a goodbye.
# Increase if calls feel like they “drop” too quickly when the caller is still on the line (default 8).
try:
    VOICE_SILENCE_MAX_RETRIES = int(os.getenv("VOICE_SILENCE_MAX_RETRIES", "8"))
except ValueError:
    VOICE_SILENCE_MAX_RETRIES = 8
VOICE_SILENCE_MAX_RETRIES = max(3, min(25, VOICE_SILENCE_MAX_RETRIES))

try:
    VOICE_STEP_MAX_RETRIES = int(os.getenv("VOICE_STEP_MAX_RETRIES", "8"))
except ValueError:
    VOICE_STEP_MAX_RETRIES = 8
# How many times we re-ask when speech doesn’t match (wrong service name, unclear yes/no, etc.) before hanging up.
VOICE_STEP_MAX_RETRIES = max(3, min(15, VOICE_STEP_MAX_RETRIES))

VOICE_LLM_FOR_SILENCE_NUDGES = os.getenv("VOICE_LLM_FOR_SILENCE_NUDGES", "").strip().lower() in (
    "1",
    "true",
    "yes",
)

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
# Optional: separate Terminal id for the lobby/kiosk reader (falls back to SQUARE_DEVICE_ID if empty)
SQUARE_KIOSK_DEVICE_ID = os.getenv("SQUARE_KIOSK_DEVICE_ID", "").strip()
# Webhook signature key + exact notification URL string from Developer Console (for HMAC verification)
SQUARE_WEBHOOK_SIGNATURE_KEY = os.getenv("SQUARE_WEBHOOK_SIGNATURE_KEY", "").strip()
SQUARE_WEBHOOK_NOTIFICATION_URL = os.getenv("SQUARE_WEBHOOK_NOTIFICATION_URL", "").strip()
# Used for Square payment link redirect (no trailing slash)
FRONTEND_BASE_URL = os.getenv("FRONTEND_BASE_URL", "http://localhost:3001")
# Point of Sale API (iPad/Android): must match Web callback URL in Square Developer Console exactly (usually https://<api>/api/v1/square/pos-callback/)
SQUARE_POS_CALLBACK_URL = os.getenv("SQUARE_POS_CALLBACK_URL", "").strip()
# If Square adds cancel_url to CheckoutOptions, set to true to send it (default off — avoids API errors)
SQUARE_CHECKOUT_SEND_CANCEL_URL = os.getenv("SQUARE_CHECKOUT_SEND_CANCEL_URL", "").strip().lower() in (
    "1",
    "true",
    "yes",
)

# Email (login verification codes, optional other mail)
EMAIL_HOST = os.environ.get("EMAIL_HOST", "").strip()
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587") or "587")
EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "true").lower() in ("1", "true", "yes")
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "").strip()
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "").strip()
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "noreply@localhost").strip()
# Patient bill / receipt emails (doctors & front desk after payment)
PATIENT_BILL_FROM_EMAIL = os.environ.get(
    "PATIENT_BILL_FROM_EMAIL", "reliefchiropracticmi@gmail.com"
).strip() or "reliefchiropracticmi@gmail.com"
SERVER_EMAIL = DEFAULT_FROM_EMAIL
if EMAIL_HOST:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"


# ---------------------------------------------------------
# CORS
# ---------------------------------------------------------
if DEBUG:
    CORS_ALLOW_ALL_ORIGINS = True
    CORS_ALLOWED_ORIGINS = []
else:
    # Production: never allow all origins
    CORS_ALLOW_ALL_ORIGINS = False
    CORS_ALLOWED_ORIGINS = [
        o.strip()
        for o in os.environ.get("CORS_ALLOWED_ORIGINS", "").split(",")
        if o.strip()
    ]

# ---------------------------------------------------------
# HIPAA Audit Logging
# ---------------------------------------------------------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "hipaa": {
            "format": "%(asctime)s HIPAA_AUDIT %(message)s",
        },
    },
    "handlers": {
        "hipaa_file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(BASE_DIR / "logs" / "hipaa_audit.log"),
            "maxBytes": 10 * 1024 * 1024,  # 10MB per file
            "backupCount": 90,  # Keep 90 files = ~6 months of logs
            "formatter": "hipaa",
        },
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "hipaa",
        },
    },
    "loggers": {
        "hipaa.audit": {
            "handlers": ["hipaa_file", "console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}

# Create logs directory if it doesn't exist
_logs_dir = BASE_DIR / "logs"
try:
    _logs_dir.mkdir(parents=True, exist_ok=True)
except OSError:
    pass

# ---------------------------------------------------------
# HIPAA Security Hardening (production only)
# ---------------------------------------------------------
if not DEBUG:
    SECURE_SSL_REDIRECT = True
    # Docker / load balancer health probes hit Gunicorn over plain HTTP inside the container.
    # Paths are matched after lstrip("/") — see SecurityMiddleware in Django.
    SECURE_REDIRECT_EXEMPT = [r"^health/?$"]
    SECURE_HSTS_SECONDS = 31536000  # 1 year
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_BROWSER_XSS_FILTER = True
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_AGE = 3600  # 1 hour auto logout (HIPAA requirement)
    CSRF_COOKIE_SECURE = True
    X_FRAME_OPTIONS = "DENY"
    # Trust X-Forwarded-Proto from nginx/reverse proxy (avoids redirect loops)
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
