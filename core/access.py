"""Who can see and change what.

* Platform admins (Django superusers) only manage portals: create, brand, suspend, invite owners.
  They have no portal of their own and see none of a portal's data.
* In a portal: the **owner** sees and changes everything, including logins. **Team members**
  see only the areas (sections) they were given, and change things only if allowed.
  **Client portal** users see only their own account's numbers, traffic and statements.

Which portal's rows a request can reach is enforced separately, by core.tenancy.
"""
from functools import wraps

from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect

from .models import SECTION_KEYS, Profile


def profile(user):
    return getattr(user, "profile", None) if user and user.is_authenticated else None


def is_platform_admin(user):
    return bool(user and user.is_authenticated and user.is_superuser)


def role(user):
    p = profile(user)
    if not p or is_platform_admin(user):
        return None
    return p.role


def client_account(user):
    """The account a client-portal user is limited to, or None for the portal's own team."""
    if role(user) == Profile.ROLE_CLIENT:
        return user.profile.account
    return None


def is_team(user):
    return role(user) in (Profile.ROLE_OWNER, Profile.ROLE_MEMBER)


def is_owner(user):
    return role(user) == Profile.ROLE_OWNER


def sections(user):
    r = role(user)
    if r == Profile.ROLE_OWNER:
        return set(SECTION_KEYS)
    if r == Profile.ROLE_MEMBER:
        return set(user.profile.sections or []) & set(SECTION_KEYS)
    return set()


def can(user, section=None, edit=False):
    """Can this user see (or, with edit, change) a section? section=None means any team page."""
    if not is_team(user):
        return False
    if section and section not in sections(user):
        return False
    if edit and not is_owner(user) and not user.profile.can_edit:
        return False
    return True


def can_edit(user):
    return is_owner(user) or (is_team(user) and user.profile.can_edit)


def _guard(request, check):
    if is_platform_admin(request.user):
        return redirect("platform")
    if not check(request.user):
        raise PermissionDenied


def require(section=None, *also):
    """The portal's own team only, with access to `section` (and any of `also`, all required).
    Any POST also needs edit rights."""
    def deco(view):
        @wraps(view)
        def wrapper(request, *a, **kw):
            needed = [s for s in (section, *also) if s]
            edit = request.method == "POST"
            resp = _guard(request, lambda u: all(can(u, s, edit) for s in needed) if needed else can(u, None, edit))
            return resp or view(request, *a, **kw)
        return wrapper
    return deco


def portal_view(section=None, client_ok=True):
    """Pages the portal's clients can also open (they see only their own account's data).
    Team members need `section`. Clients can't POST to these."""
    def deco(view):
        @wraps(view)
        def wrapper(request, *a, **kw):
            def ok(u):
                if role(u) == Profile.ROLE_CLIENT:
                    return client_ok and request.method != "POST" and client_account(u) is not None
                return can(u, section, edit=request.method == "POST")
            resp = _guard(request, ok)
            return resp or view(request, *a, **kw)
        return wrapper
    return deco


def owner_required(view):
    @wraps(view)
    def wrapper(request, *a, **kw):
        return _guard(request, is_owner) or view(request, *a, **kw)
    return wrapper


def platform_admin_required(view):
    @wraps(view)
    def wrapper(request, *a, **kw):
        if not is_platform_admin(request.user):
            raise PermissionDenied
        return view(request, *a, **kw)
    return wrapper


# Kept for older code: the portal's team, any section.
staff_required = require()


def machine_endpoint(view):
    """Endpoints called by Jasmin, FreeSWITCH or a supplier's / customer's system, which check
    their own token or API key. No login; runs across portals, so the code must narrow to the
    caller's portal with tenancy.use_tenant() before reading or writing portal data."""
    from django.contrib.auth.decorators import login_not_required

    from .tenancy import all_tenants

    @wraps(view)
    def wrapper(request, *a, **kw):
        with all_tenants():
            return view(request, *a, **kw)
    return login_not_required(wrapper)
