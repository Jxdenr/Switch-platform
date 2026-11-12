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


class Profile(models.Model):
    ROLE_ADMIN = "admin"
    ROLE_OPS = "ops"
    ROLE_CLIENT = "client"
    ROLES = [(ROLE_ADMIN, "Admin"), (ROLE_OPS, "Operations"), (ROLE_CLIENT, "Client portal")]

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    role = models.CharField(max_length=10, choices=ROLES, default=ROLE_OPS)
    account = models.ForeignKey(
        "crm.Account", null=True, blank=True, on_delete=models.CASCADE,
        help_text="Set for client portal users: they only see this account's data.",
    )
    colourway = models.CharField(max_length=12, default="harbour")
    layout = models.CharField(max_length=12, default="sidebar")

    def __str__(self):
        return f"{self.user} ({self.role})"


class ActivityLog(models.Model):
    """Every change made through the UI, API or assistant."""

    at = models.DateTimeField(auto_now_add=True, db_index=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    via = models.CharField(max_length=10, default="ui")  # ui, api, assistant, system
    action = models.CharField(max_length=60)
    target = models.CharField(max_length=200, blank=True)
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-at"]


def log(user, action, target="", via="ui", **detail):
    ActivityLog.objects.create(
        user=user if getattr(user, "is_authenticated", False) else None,
        via=via, action=action, target=str(target)[:200], detail=detail,
    )
