"""Who can see and change what.

Admin and ops see everything. Client-portal users only see their own account's numbers
and messages, and cannot change anything except their own HTTP endpoints.
"""
from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied

from .models import Profile


def role(user):
    if user.is_superuser:
        return Profile.ROLE_ADMIN
    p = getattr(user, "profile", None)
    return p.role if p else Profile.ROLE_OPS


def client_account(user):
    """The account a client-portal user is limited to, or None for staff."""
    if role(user) == Profile.ROLE_CLIENT:
        return user.profile.account
    return None


def staff_required(view):
    @login_required
    @wraps(view)
    def wrapper(request, *a, **kw):
        if role(request.user) == Profile.ROLE_CLIENT:
            raise PermissionDenied
        return view(request, *a, **kw)
    return wrapper
