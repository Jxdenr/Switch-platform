"""Per-request portal selection and the two-factor gate.

Order in settings.MIDDLEWARE: after AuthenticationMiddleware and Django's
LoginRequiredMiddleware (which turns away anonymous users from every page that isn't
explicitly marked @login_not_required).
"""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.shortcuts import redirect

from . import tenancy
from .security import needs_2fa

# Paths a signed-in user who still has to set up two-factor may open.
SETUP_PATHS = ("/account/security/", "/logout/", "/static/")


class TenantMiddleware:
    """Opens exactly one portal for the request (or none), and closes it afterwards."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        request.tenant = None
        value = tenancy.DENY
        if user.is_authenticated:
            if user.is_superuser:
                # Platform admins see no portal's data in the app. Django's own admin (off unless
                # DJANGO_ADMIN=1) is a raw database tool and reaches every portal.
                if settings.DJANGO_ADMIN_ENABLED and request.path.startswith("/admin/"):
                    value = tenancy.ALL
            else:
                p = getattr(user, "profile", None)
                t = p.tenant if p else None
                if not t or not t.active:
                    logout(request)
                    messages.error(request, "This portal is suspended. Please contact your provider."
                                   if t else "This login isn't linked to a portal.")
                    return redirect(settings.LOGIN_URL)
                request.tenant = t
                value = t.pk
        token = tenancy.activate(value)
        try:
            response = self.get_response(request)
        finally:
            tenancy.deactivate(token)
        if getattr(response, "streaming", False):
            response.streaming_content = _in_portal(response.streaming_content, value)
        return response


def _in_portal(iterable, value):
    """Streamed responses (CSV exports) run their queries after the view returns."""
    token = tenancy.activate(value)
    try:
        yield from iterable
    finally:
        tenancy.deactivate(token)


class TwoFactorSetupMiddleware:
    """Anyone who must use two-factor and hasn't set it up yet can only reach the setup page."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        if (user.is_authenticated and needs_2fa(user) and not _has_2fa(user)
                and not request.path.startswith(SETUP_PATHS)):
            return redirect("/account/security/?setup=1")
        return self.get_response(request)


def _has_2fa(user):
    p = getattr(user, "profile", None)
    return bool(p and p.totp_enabled)
