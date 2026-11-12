"""Django settings. Configure with environment variables (see .env.example)."""
import os
from pathlib import Path

import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-insecure-key")
DEBUG = os.environ.get("DEBUG", "1") == "1"
ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
CSRF_TRUSTED_ORIGINS = [o for o in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",") if o]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "core",
    "crm",
    "inventory",
    "sms",
    "voice",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "switch.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context.ui",
                "core.context.nav",
            ],
        },
    },
]

WSGI_APPLICATION = "switch.wsgi.application"

DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}", conn_max_age=600
    )
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

LANGUAGE_CODE = "en-gb"
TIME_ZONE = os.environ.get("TIME_ZONE", "UTC")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

# Inbound SMS from Jasmin is posted to /ingest/jasmin/ with this shared secret.
INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "dev-ingest-token")
# Jasmin management (jCli) used by `manage.py sync_jasmin`.
JASMIN_JCLI_HOST = os.environ.get("JASMIN_JCLI_HOST", "jasmin")
JASMIN_JCLI_PORT = int(os.environ.get("JASMIN_JCLI_PORT", "8990"))
JASMIN_JCLI_USER = os.environ.get("JASMIN_JCLI_USER", "jcliadmin")
JASMIN_JCLI_PASSWORD = os.environ.get("JASMIN_JCLI_PASSWORD", "jclipwd")
# URL Jasmin calls for every inbound message (our ingest endpoint, reachable from the Jasmin container).
JASMIN_MO_CALLBACK_URL = os.environ.get("JASMIN_MO_CALLBACK_URL", "http://web:8000/ingest/jasmin/")
FORWARD_MAX_ATTEMPTS = int(os.environ.get("FORWARD_MAX_ATTEMPTS", "5"))
FORWARD_TIMEOUT = float(os.environ.get("FORWARD_TIMEOUT", "10"))
