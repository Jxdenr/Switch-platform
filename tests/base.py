"""Every test runs inside a portal, as the web app does for a signed-in user."""
from django.contrib.auth.models import User

from core import tenancy
from core.models import Profile, SECTION_KEYS, Tenant


def make_portal(name="Test portal", **kw):
    kw.setdefault("company_name", "Test Co")
    return Tenant.objects.create(name=name, **kw)


def make_login(username, tenant, role="owner", password="pass-12345678", **profile):
    """A login in `tenant`. Members get every section and edit rights unless told otherwise."""
    with tenancy.use_tenant(tenant):
        u = User.objects.create_user(username, f"{username}@example.com", password)
    if role == Profile.ROLE_MEMBER:
        profile.setdefault("sections", SECTION_KEYS)
        profile.setdefault("can_edit", True)
    Profile.objects.filter(user=u).update(tenant=tenant, role=role, **profile)
    u.refresh_from_db()
    return u


class PortalMixin:
    """Creates self.tenant and makes it the current portal for the whole test."""

    def _pre_setup(self, *a, **kw):
        super()._pre_setup(*a, **kw)
        self.tenant = make_portal()
        self._tenant_token = tenancy.activate(self.tenant.pk)

    def _post_teardown(self, *a, **kw):
        tenancy.deactivate(self._tenant_token)
        super()._post_teardown(*a, **kw)
