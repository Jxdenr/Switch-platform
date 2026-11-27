"""Sign-in protection: lockout after repeated failures, and the client's real IP."""
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .models import LoginAttempt

# (window, max failures) per username+IP, per IP, per username. The per-username limit is
# deliberately loose so nobody can lock you out of your own account from their own IP.
WINDOW = timedelta(minutes=15)
PER_PAIR, PER_IP, PER_USER, USER_WINDOW = 5, 30, 50, timedelta(hours=1)


def client_ip(request):
    """REMOTE_ADDR, or behind our own proxy (TRUST_PROXY=1) the address the proxy saw: the
    *last* X-Forwarded-For entry. Earlier entries are whatever the client typed, so never use them."""
    if getattr(settings, "TRUST_PROXY", False):
        fwd = [x.strip() for x in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",") if x.strip()]
        if fwd:
            return fwd[-1]
    return request.META.get("REMOTE_ADDR") or None


def locked(username, ip):
    now = timezone.now()
    fails = LoginAttempt.objects.filter(ok=False)
    username = (username or "").strip().lower()
    if ip and fails.filter(at__gte=now - WINDOW, ip=ip).count() >= PER_IP:
        return True
    if username and fails.filter(at__gte=now - WINDOW, ip=ip, username=username).count() >= PER_PAIR:
        return True
    if username and fails.filter(at__gte=now - USER_WINDOW, username=username).count() >= PER_USER:
        return True
    return False


def record(username, ip, ok, stage="password"):
    LoginAttempt.objects.create(username=(username or "").strip().lower()[:150], ip=ip, ok=ok, stage=stage)
    # Keep the table small: drop entries older than a month now and then.
    if not ok and LoginAttempt.objects.count() % 500 == 0:
        LoginAttempt.objects.filter(at__lt=timezone.now() - timedelta(days=30)).delete()


def needs_2fa(user):
    """Platform admins must use an authenticator code. Other roles listed in REQUIRE_2FA too."""
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    p = getattr(user, "profile", None)
    return bool(p and p.role in getattr(settings, "REQUIRE_2FA_ROLES", ()))
