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
    http_user = models.ForeignKey("crm.HttpUser", null=True, blank=True, on_delete=models.SET_NULL,
                                  help_text="Supplier sent it to us over HTTP")
    client = models.ForeignKey("crm.Account", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    http_endpoint = models.ForeignKey("crm.HttpEndpoint", null=True, blank=True, on_delete=models.SET_NULL)
    client_bind = models.ForeignKey("crm.SmppUser", null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="delivered_messages",
                                    help_text="Client receives it over SMPP on this bind")
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
    block_rule = models.ForeignKey("BlockRule", null=True, blank=True, on_delete=models.SET_NULL, related_name="hits")
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

    @property
    def received_via(self):
        if self.http_user_id:
            return "HTTP"
        return "SMPP" if (self.smpp_user_id or self.smsc_id) else ""

    @property
    def delivered_via(self):
        return "HTTP" if self.http_endpoint_id else ("SMPP" if self.client_bind_id else "")


class BlockRule(models.Model):
    """Replaces Restrictions, SMS Restrictions and Black List on the old platform.

    A rule applies to everyone, to one supplier's traffic, to one client's traffic, or to one
    billing group. Most rules block (number, prefix, sender ID); a limit rule caps how many SMS
    one destination number may receive per hour, which catches SMS pumping early."""

    NUMBER, PREFIX, SENDER, LIMIT, CONTENT = "number", "prefix", "sender", "limit", "content"
    TYPES = [(SENDER, "Block a sender ID"), (PREFIX, "Block a number prefix"), (NUMBER, "Block one number"),
             (LIMIT, "Limit SMS per number per hour"), (CONTENT, "Block text containing")]
    ALL, SUPPLIER, CLIENT, GROUP, CUSTOMER = "all", "supplier", "client", "group", "customer"
    SCOPES = [(ALL, "Everyone"), (SUPPLIER, "A supplier"), (CLIENT, "A client"), (GROUP, "A billing group"),
              (CUSTOMER, "A wholesale customer")]

    rule_type = models.CharField(max_length=7, choices=TYPES)
    value = models.CharField(max_length=80)
    scope = models.CharField("Applies to", max_length=8, choices=SCOPES, default=ALL)
    account = models.ForeignKey("crm.Account", null=True, blank=True, on_delete=models.CASCADE,
                                help_text="The supplier or client this rule applies to")
    billing_group = models.ForeignKey("inventory.BillingGroup", null=True, blank=True, on_delete=models.CASCADE,
                                      related_name="block_rules")
    reason = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["scope", "rule_type", "value"]

    def __str__(self):
        if self.rule_type == self.LIMIT:
            return f"Max {self.value} SMS per number per hour"
        return f"{self.get_rule_type_display()}: {self.value}"

    @property
    def target(self):
        if self.scope == self.GROUP:
            return self.billing_group
        if self.scope in (self.SUPPLIER, self.CLIENT, self.CUSTOMER):
            return self.account
        return None

    def target_label(self):
        t = self.target
        return f"{self.get_scope_display()[2:].capitalize()}: {t}" if t else "Everyone"

    @classmethod
    def applying(cls, supplier=None, client=None, billing_group=None, customer=None):
        q = models.Q(scope=cls.ALL)
        if customer:
            q |= models.Q(scope=cls.CUSTOMER, account=customer)
        if supplier:
            q |= models.Q(scope=cls.SUPPLIER, account=supplier)
        if client:
            q |= models.Q(scope=cls.CLIENT, account=client)
        if billing_group:
            q |= models.Q(scope=cls.GROUP, billing_group=billing_group)
        return cls.objects.filter(q)

    def matches(self, source, destination, body=""):
        """Number, prefix, sender and content rules. Limit rules are counted by the caller."""
        if self.rule_type == self.NUMBER:
            return destination == self.value
        if self.rule_type == self.PREFIX:
            return destination.startswith(self.value)
        if self.rule_type == self.SENDER:
            return source.lower() == self.value.lower()
        if self.rule_type == self.CONTENT:
            return bool(self.value) and self.value.lower() in (body or "").lower()
        return False

    @classmethod
    def blocks(cls, source, destination, client=None, supplier=None, billing_group=None, at=None, body=""):
        for r in cls.applying(supplier, client, billing_group):
            if r.matches(source, destination, body):
                return r
            if r.rule_type == cls.LIMIT and r.value.isdigit():
                from datetime import timedelta
                from django.utils import timezone
                at = at or timezone.now()
                recent = Message.objects.filter(destination=destination, received_at__gt=at - timedelta(hours=1),
                                                received_at__lte=at).exclude(status=Message.BLOCKED).count()
                if recent >= int(r.value):
                    return r
        return None
