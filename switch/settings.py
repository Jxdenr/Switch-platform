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

# Statements and invoices. COMPANY_DETAILS appears on every document: address, VAT/company
# number and the bank details suppliers should pay into (use \n for new lines in .env).
COMPANY_NAME = os.environ.get("COMPANY_NAME", "Your Company Ltd")
DEFAULT_CURRENCY = os.environ.get("DEFAULT_CURRENCY", "GBP")
COMPANY_DETAILS = os.environ.get("COMPANY_DETAILS", "").replace("\\n", "\n")
STATEMENT_SEND_DAY = int(os.environ.get("STATEMENT_SEND_DAY", "1"))

# Email (statements). Without EMAIL_HOST, emails are printed to the log instead of sent.
EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "1") == "1"
EMAIL_BACKEND = ("django.core.mail.backends.smtp.EmailBackend" if EMAIL_HOST
                 else "django.core.mail.backends.console.EmailBackend")
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "billing@example.com")

# Built-in SMPP server: suppliers bind to send us SMS, clients bind to receive theirs.
SMPP_SERVER_HOST = os.environ.get("SMPP_SERVER_HOST", "0.0.0.0")
SMPP_SERVER_PORT = int(os.environ.get("SMPP_SERVER_PORT", "2775"))
