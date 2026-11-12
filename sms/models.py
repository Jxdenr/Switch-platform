"""Inbound SMS records, client forwarding and blocking rules."""
import uuid

from django.db import models


class Message(models.Model):
    RECEIVED, FORWARDED, FAILED, NO_ROUTE, BLOCKED = "received", "forwarded", "failed", "no_route", "blocked"
    STATUSES = [(RECEIVED, "Received"), (FORWARDED, "Forwarded"), (FAILED, "Forward failed"),
                (NO_ROUTE, "No route"), (BLOCKED, "Blocked")]

    uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    received_at = models.DateTimeField(db_index=True)
    supplier = models.ForeignKey("crm.Account", null=True, on_delete=models.SET_NULL, related_name="+")
    smpp_user = models.ForeignKey("crm.SmppUser", null=True, blank=True, on_delete=models.SET_NULL)
    smsc = models.ForeignKey("crm.Smsc", null=True, blank=True, on_delete=models.SET_NULL)
    client = models.ForeignKey("crm.Account", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    http_endpoint = models.ForeignKey("crm.HttpEndpoint", null=True, blank=True, on_delete=models.SET_NULL)
    number = models.ForeignKey("inventory.Number", null=True, blank=True, on_delete=models.SET_NULL)
    billing_group = models.ForeignKey("inventory.BillingGroup", null=True, blank=True, on_delete=models.SET_NULL)
    source = models.CharField(max_length=32, db_index=True)
    destination = models.CharField(max_length=32, db_index=True)
    source_ton = models.PositiveSmallIntegerField(null=True, blank=True)
    source_npi = models.PositiveSmallIntegerField(null=True, blank=True)
    dest_ton = models.PositiveSmallIntegerField(null=True, blank=True)
    dest_npi = models.PositiveSmallIntegerField(null=True, blank=True)
    registered_delivery = models.BooleanField(default=False)
    body = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=STATUSES, default=RECEIVED, db_index=True)
    forward_attempts = models.PositiveSmallIntegerField(default=0)
    forwarded_at = models.DateTimeField(null=True, blank=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True)
    # Rates are copied in when the message arrives so later rate changes never rewrite history.
    currency = models.CharField(max_length=3, blank=True)
    payment_terms = models.CharField(max_length=30, blank=True)
    rate_in = models.DecimalField(max_digits=10, decimal_places=5, default=0)
    rate_out = models.DecimalField(max_digits=10, decimal_places=5, default=0)

    class Meta:
        ordering = ["-received_at"]
        indexes = [models.Index(fields=["client", "received_at"]), models.Index(fields=["supplier", "received_at"])]

    def __str__(self):
        return f"{self.source} -> {self.destination}"


class BlockRule(models.Model):
    """Replaces Restrictions, SMS Restrictions and Black List on the old platform."""

    NUMBER, PREFIX, SENDER = "number", "prefix", "sender"
    TYPES = [(NUMBER, "Destination number"), (PREFIX, "Destination prefix"), (SENDER, "Sender ID")]

    rule_type = models.CharField(max_length=7, choices=TYPES)
    value = models.CharField(max_length=40)
    account = models.ForeignKey("crm.Account", null=True, blank=True, on_delete=models.CASCADE,
                                help_text="Blank = applies to everyone")
    reason = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.get_rule_type_display()}: {self.value}"

    @classmethod
    def blocks(cls, source, destination, client=None):
        qs = cls.objects.filter(models.Q(account__isnull=True) | models.Q(account=client))
        for r in qs:
            if r.rule_type == cls.NUMBER and destination == r.value:
                return r
            if r.rule_type == cls.PREFIX and destination.startswith(r.value):
                return r
            if r.rule_type == cls.SENDER and source.lower() == r.value.lower():
                return r
        return None
