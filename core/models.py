"""Shared lookups and user roles.

These mirror the settings screens of the original platform (Currencies,
Number Types, Payment Terms, Times Of Day) so billing behaves the same way.
"""
from django.conf import settings
from django.db import models


class Country(models.Model):
    name = models.CharField(max_length=100, unique=True)
    iso2 = models.CharField(max_length=2, blank=True)
    dial_code = models.CharField(max_length=8, blank=True, help_text="E.g. 44 for the UK")
    continent = models.CharField(max_length=20, blank=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "countries"

    def __str__(self):
        return self.name


class Currency(models.Model):
    code = models.CharField(max_length=3, unique=True)
    name = models.CharField(max_length=40)
    symbol = models.CharField(max_length=4, blank=True)

    class Meta:
        ordering = ["code"]
        verbose_name_plural = "currencies"

    def __str__(self):
        return self.code


class NumberType(models.Model):
    title = models.CharField(max_length=60, unique=True)

    def __str__(self):
        return self.title


class PaymentTerms(models.Model):
    """E.g. "30/45": paid 30 days after a 45 day interval."""

    title = models.CharField(max_length=30, unique=True)
    interval_1 = models.PositiveIntegerField(default=30)
    interval_2 = models.PositiveIntegerField(default=30)

    class Meta:
        ordering = ["title"]
        verbose_name_plural = "payment terms"

    def __str__(self):
        return self.title


class TimeOfDay(models.Model):
    """Rate band. For SMS everything normally uses "All Times"."""

    title = models.CharField(max_length=30, unique=True)
    days = models.CharField(max_length=40, default="Mon-Sun")
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)

    def __str__(self):
        return self.title


class Tenant(models.Model):
    """One portal: a separate business on this switch, with its own suppliers, clients, numbers,
    traffic and logins. Every business record points at exactly one portal (see core.tenancy)."""

    name = models.CharField(max_length=120, unique=True, help_text="Shown at the top of the portal.")
    active = models.BooleanField(default=True, help_text="Suspended portals can't sign in. Traffic keeps flowing.")
    colourway = models.CharField(max_length=12, default="harbour", help_text="Default colour scheme for the portal.")
    # Printed on this portal's statements and invoices, and used for its emails.
    company_name = models.CharField(max_length=120, blank=True, help_text="Legal name on statements and invoices.")
    company_details = models.TextField(
        blank=True, help_text="Address, company and VAT numbers, and the bank details suppliers should pay "
                              "into. Printed on every statement and invoice.")
    billing_email = models.EmailField(blank=True, help_text="Replies to statement emails go here.")
    default_currency = models.CharField(max_length=3, default="GBP")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def legal_name(self):
        return self.company_name or self.name


# What a team member can be given. Owners and the portal's own clients don't use these.
SECTIONS = [
    ("customers", "Customers", "Suppliers, clients and their binds"),
    ("numbers", "Numbers", "Billing groups, numbers and allocation"),
    ("sms", "SMS", "SMS records, live ticker and blocking rules"),
    ("voice", "Voice", "Call records"),
    ("wholesale", "Wholesale", "Wholesale traffic, routes and vendors"),
    ("money", "Money", "Rates, due in / due out, stats, statements and invoices"),
]
SECTION_KEYS = [k for k, _, _ in SECTIONS]


class Profile(models.Model):
    ROLE_OWNER = "owner"
    ROLE_MEMBER = "member"
    ROLE_CLIENT = "client"
    ROLES = [(ROLE_OWNER, "Owner"), (ROLE_MEMBER, "Team member"), (ROLE_CLIENT, "Client portal")]

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    tenant = models.ForeignKey(Tenant, null=True, blank=True, on_delete=models.CASCADE, related_name="profiles",
                               help_text="The portal this login belongs to. Empty only for platform admins.")
    role = models.CharField(max_length=10, choices=ROLES, default=ROLE_MEMBER)
    sections = models.JSONField(default=list, blank=True, help_text="Team members: the areas they can see.")
    can_edit = models.BooleanField(default=False, help_text="Team members: can change things, not only view.")
    account = models.ForeignKey(
        "crm.Account", null=True, blank=True, on_delete=models.CASCADE,
        help_text="Set for client portal users: they only see this account's data.",
    )
    colourway = models.CharField(max_length=12, blank=True, help_text="Blank = the portal's colour scheme.")
    layout = models.CharField(max_length=12, default="sidebar")
    # Two-factor sign-in (authenticator app, RFC 6238). The secret is base32.
    totp_secret = models.CharField(max_length=64, blank=True)
    totp_enabled = models.BooleanField(default=False)
    totp_last_step = models.BigIntegerField(default=0)  # stops a code being used twice
    invited_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")

    def __str__(self):
        return f"{self.user} ({self.role})"


class LoginAttempt(models.Model):
    """Failed and successful sign-ins, for lockout and the security log."""

    at = models.DateTimeField(auto_now_add=True, db_index=True)
    username = models.CharField(max_length=150, db_index=True)
    ip = models.GenericIPAddressField(null=True, blank=True, db_index=True)
    ok = models.BooleanField(default=False)
    stage = models.CharField(max_length=10, default="password")  # password, code

    class Meta:
        ordering = ["-at"]


class ActivityLog(models.Model):
    """Every change made through the UI, API or assistant."""

    at = models.DateTimeField(auto_now_add=True, db_index=True)
    tenant = models.ForeignKey(Tenant, null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    via = models.CharField(max_length=10, default="ui")  # ui, api, assistant, system
    action = models.CharField(max_length=60)
    target = models.CharField(max_length=200, blank=True)
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-at"]


def log(user, action, target="", via="ui", tenant=None, **detail):
    from .tenancy import current_id
    ActivityLog.objects.create(
        tenant_id=getattr(tenant, "pk", tenant) or current_id(),
        user=user if getattr(user, "is_authenticated", False) else None,
        via=via, action=action, target=str(target)[:200], detail=detail,
    )
