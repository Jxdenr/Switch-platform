"""Separate portals (tenants) on one switch.

Every business record belongs to exactly one portal. Isolation fails closed:

* Each tenant-owned model's default manager (``Model.objects``) filters to the *current portal*.
  In a web request the middleware sets it from the signed-in user. If nothing set it
  (an anonymous request, a background thread in the web process), any query raises
  ``TenantNotSet`` instead of returning everyone's data.
* Saving a record fills in its portal from its parent (an SMS from its supplier, a number from
  its billing group) or from the current portal, and refuses a record whose parents sit in
  another portal, or that is being written into a portal other than the current one.
* System code that genuinely works across portals (SMPP server, ingest endpoints, workers,
  management commands) runs under ``all_tenants()``; it should narrow to one portal with
  ``use_tenant()`` as soon as it knows which one.
* ``Model.unscoped`` is the escape hatch. Only use it in platform-level code, with a comment.
"""
import contextvars
import os
from contextlib import contextmanager

from django.db import models
from django.db.models import Q, sql


class TenantNotSet(RuntimeError):
    """A tenant-owned table was queried with no portal selected."""


class CrossTenantError(ValueError):
    """A record would point at, or be written into, another portal."""


ALL = "all"      # system code: every portal
DENY = "deny"    # nothing selected: refuse every query
_current = contextvars.ContextVar("switch_tenant", default=None)
# What an unset context means in this process. manage.py sets ALL (commands and workers);
# the web server (wsgi/asgi) leaves DENY, so only the middleware can open a portal.
# (The web server's wsgi.py/asgi.py removes the variable before loading Django.)
_process_default = ALL if os.environ.get("SWITCH_CLI_ALL_PORTALS") == "1" else DENY


def set_process_default(value):
    global _process_default
    assert value in (ALL, DENY)
    _process_default = value


def current():
    """ALL, DENY, or the current portal's id."""
    v = _current.get()
    return _process_default if v is None else v


def current_id():
    v = current()
    return v if isinstance(v, int) else None


@contextmanager
def use_tenant(tenant):
    """Run a block inside one portal (a Tenant, its id, ALL or DENY)."""
    value = tenant if tenant in (ALL, DENY) else int(getattr(tenant, "pk", tenant))
    token = _current.set(value)
    try:
        yield
    finally:
        _current.reset(token)


def all_tenants():
    return use_tenant(ALL)


def activate(value):
    """For middleware: set and return a token to reset with ``deactivate``."""
    return _current.set(value)


def deactivate(token):
    _current.reset(token)


def _parent_fields(model):
    return [f for f in model._meta.concrete_fields
            if f.many_to_one or f.one_to_one
            if f.related_model is not None and issubclass(f.related_model, TenantOwned)]


def _parent_tenants(obj):
    """{field name: tenant id} for every tenant-owned parent this object points at."""
    out = {}
    for f in _parent_fields(type(obj)):
        pk = getattr(obj, f.attname)
        if pk is None:
            continue
        if f.is_cached(obj):
            out[f.name] = getattr(obj, f.name).tenant_id
        else:
            out[f.name] = (f.related_model.unscoped.filter(pk=pk)
                           .values_list("tenant_id", flat=True).first())
    return out


def stamp(obj):
    """Give ``obj`` its portal and check it against its parents and the current portal."""
    parents = _parent_tenants(obj)
    if obj.tenant_id is None:
        found = {t for t in parents.values() if t is not None}
        if len(found) == 1:
            obj.tenant_id = found.pop()
        elif current_id() is not None:
            obj.tenant_id = current_id()
        else:
            raise CrossTenantError(f"Can't tell which portal this {obj._meta.verbose_name} belongs to.")
    for name, t in parents.items():
        if t != obj.tenant_id:
            raise CrossTenantError(f"{obj._meta.verbose_name}: {name} belongs to another portal.")
    cur = current()
    if cur == DENY:
        raise TenantNotSet("No portal selected.")
    if cur != ALL and cur != obj.tenant_id:
        raise CrossTenantError(f"Refusing to write a {obj._meta.verbose_name} into another portal.")


class TenantQuery(sql.Query):
    """SQL query that adds ``tenant_id = <current portal>`` when it is compiled, not when it is
    built. Querysets built at import time (form dropdowns, module constants) are therefore
    still filtered to whichever portal is current when they run, and subqueries
    (``Exists``, ``Subquery``, ``pk__in=qs``) are filtered too."""

    tenant_applied = None

    def scoped(self):
        cur = current()
        if cur == ALL:
            return self
        if cur == DENY:
            raise TenantNotSet(f"{self.model.__name__} queried with no portal selected.")
        if self.tenant_applied == cur:
            return self
        q = self.clone()
        q.add_q(Q(tenant_id=cur))
        q.tenant_applied = cur
        return q

    def get_compiler(self, using=None, connection=None, elide_empty=True):
        q = self.scoped()
        return sql.Query.get_compiler(q, using, connection, elide_empty)


class TenantQuerySet(models.QuerySet):
    def __init__(self, model=None, query=None, using=None, hints=None):
        super().__init__(model, query or TenantQuery(model), using, hints)

    def bulk_create(self, objs, *args, **kwargs):
        objs = list(objs)
        for o in objs:
            stamp(o)
        return super().bulk_create(objs, *args, **kwargs)

    # update() turns the query into an UpdateQuery, which compiles without the hook above,
    # so scope it first.
    def update(self, **kwargs):
        self.query = self.query.scoped()
        return super().update(**kwargs)

    update.alters_data = True

    def _update(self, values):
        self.query = self.query.scoped()
        return super()._update(values)

    _update.alters_data = True
    _update.queryset_only = False


class TenantManager(models.Manager.from_queryset(TenantQuerySet)):
    pass


class TenantOwned(models.Model):
    """Mixin: the record belongs to one portal and is only visible inside it."""

    tenant = models.ForeignKey("core.Tenant", on_delete=models.PROTECT, related_name="+", editable=False)

    objects = TenantManager()
    unscoped = models.Manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        stamp(self)
        super().save(*args, **kwargs)

    def validate_unique(self, exclude=None):
        # Check uniqueness per portal (tenant is never a form field, so put it back) and check
        # platform-wide unique fields (SMPP usernames, API keys) against every portal.
        if self.tenant_id is None and current_id() is not None:
            self.tenant_id = current_id()
        exclude = set(exclude or ()) - {"tenant"}
        with all_tenants():
            super().validate_unique(exclude=exclude)
