"""Helpers for the migrations that split data into portals (tenants)."""
from django.conf import settings


def default_tenant(apps):
    """The portal existing data moves into: the first one, created if needed."""
    Tenant = apps.get_model("core", "Tenant")
    t = Tenant.objects.order_by("pk").first()
    if not t:
        name = getattr(settings, "COMPANY_NAME", "") or "My portal"
        t = Tenant.objects.create(name=name, company_name=name,
                                  company_details=getattr(settings, "COMPANY_DETAILS", ""),
                                  default_currency=getattr(settings, "DEFAULT_CURRENCY", "GBP"))
    return t


def fill_tenant(app_label, *model_names):
    """RunPython operation: put every existing row of these models into the default portal."""
    def run(apps, schema_editor):
        models = [apps.get_model(app_label, n) for n in model_names]
        if not any(M.objects.filter(tenant__isnull=True).exists() for M in models):
            return
        t = default_tenant(apps)
        for M in models:
            M.objects.filter(tenant__isnull=True).update(tenant=t)
    return run
