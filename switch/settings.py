"""Django settings. Configure with environment variables (see .env.example)."""
import os
from pathlib import Path

import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-insecure-key")
DEBUG = os.environ.get("DEBUG", "1") == "1"
if not DEBUG and (SECRET_KEY == "dev-only-insecure-key" or len(SECRET_KEY) < 40):
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured("Set SECRET_KEY to a long random value in .env before running with DEBUG=0.")
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
    "wholesale",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    # Every page needs a login unless the view is marked @login_not_required.
    "django.contrib.auth.middleware.LoginRequiredMiddleware",
    "core.middleware.TenantMiddleware",
    "core.middleware.TwoFactorSetupMiddleware",
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

# Argon2 for new and changed passwords; older PBKDF2 hashes still work and are upgraded on sign-in.
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
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

# Sessions: signed out after 12 hours, cookies only over HTTPS when DEBUG is off.
SESSION_COOKIE_AGE = int(os.environ.get("SESSION_HOURS", "12")) * 3600
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
PASSWORD_RESET_TIMEOUT = 3 * 86400  # invite and reset links last 3 days
# Roles that must use an authenticator code, besides platform admins (always). E.g. "owner".
REQUIRE_2FA_ROLES = [r for r in os.environ.get("REQUIRE_2FA_ROLES", "").split(",") if r]
# Behind our own TLS proxy (Caddy): trust its X-Forwarded-Proto and last X-Forwarded-For entry.
TRUST_PROXY = os.environ.get("TRUST_PROXY", "0" if DEBUG else "1") == "1"
if TRUST_PROXY:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
if not DEBUG:
    SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = True
    SECURE_SSL_REDIRECT = os.environ.get("SECURE_SSL_REDIRECT", "1") == "1"
    SECURE_REDIRECT_EXEMPT = [r"^health/$", r"^ingest/", r"^api/"]  # machine endpoints
    SECURE_HSTS_SECONDS = int(os.environ.get("HSTS_SECONDS", "31536000"))
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
# Django's built-in database admin reaches every portal's data. Off unless DJANGO_ADMIN=1.
DJANGO_ADMIN_ENABLED = os.environ.get("DJANGO_ADMIN", "1" if DEBUG else "0") == "1"

# Inbound SMS from Jasmin is posted to /ingest/jasmin/ with this shared secret.
INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "dev-ingest-token")
if not DEBUG and (INGEST_TOKEN == "dev-ingest-token" or INGEST_TOKEN.startswith("CHANGE") or len(INGEST_TOKEN) < 24):
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured("Set INGEST_TOKEN to a long random value in .env before running with DEBUG=0.")
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

# Wholesale SMS (customers send us traffic, we route it to vendors). All wholesale rates and
# balances are in this one currency.
WHOLESALE_CURRENCY = os.environ.get("WHOLESALE_CURRENCY", DEFAULT_CURRENCY)
# A message with a vendor and no receipt after this long is marked UNKNOWN ("no receipt from route").
WHOLESALE_DLR_TIMEOUT_HOURS = int(os.environ.get("WHOLESALE_DLR_TIMEOUT_HOURS", "72"))
# A queued message no vendor could take within this many seconds is rejected (and refunded).
WHOLESALE_QUEUE_TIMEOUT = int(os.environ.get("WHOLESALE_QUEUE_TIMEOUT", "600"))
WHOLESALE_MAX_VENDOR_ATTEMPTS = int(os.environ.get("WHOLESALE_MAX_VENDOR_ATTEMPTS", "4"))
WHOLESALE_DLR_MAX_ATTEMPTS = int(os.environ.get("WHOLESALE_DLR_MAX_ATTEMPTS", "8"))
# Message text is personal data: wholesale message text is cleared after this many days (metadata stays).
WHOLESALE_BODY_RETENTION_DAYS = int(os.environ.get("WHOLESALE_BODY_RETENTION_DAYS", "30"))
