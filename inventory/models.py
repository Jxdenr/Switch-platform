"""Billing groups, numbers and allocations.

Flow, same as the original platform:
1. A supplier gives you a range of numbers -> create a Billing Group for them
   (country, routing type, currency, up to 3 payment terms with a rate in/out).
2. Add the numbers to the group (range + quantity, a pasted list, or a CSV).
3. Allocate numbers to a client, choosing the terms and the client's rate.
   Inbound SMS/calls to an allocated number are routed to that client.
"""
from django.db import models
from django.utils import timezone

SMS, VOICE = "sms", "voice"
CHANNELS = [(SMS, "SMS"), (VOICE, "Voice")]


class BillingGroup(models.Model):
    VOICE, SMS, BOTH = "voice", "sms", "both"
    ROUTING_TYPES = [(SMS, "SMS"), (VOICE, "Voice"), (BOTH, "SMS & voice")]

    supplier = models.ForeignKey("crm.Account", on_delete=models.PROTECT, related_name="billing_groups",
                                 limit_choices_to={"is_supplier": True})
    title = models.CharField(max_length=120)
    routing_type = models.CharField(max_length=5, choices=ROUTING_TYPES, default=SMS,
                                    help_text="Numbers in this group can be used for SMS, voice, or both.")
    number_type = models.ForeignKey("core.NumberType", null=True, blank=True, on_delete=models.PROTECT)
    country = models.ForeignKey("core.Country", on_delete=models.PROTECT)
    network = models.CharField(max_length=60, blank=True)
    currency = models.ForeignKey("core.Currency", on_delete=models.PROTECT)
    blocked = models.BooleanField(default=False)
    active = models.BooleanField(default=True)
    note = models.TextField(blank=True)
    # Voice-only settings (kept so voice can be added without a schema change)
    billing_increment_first = models.PositiveIntegerField(default=1)
    billing_increment_next = models.PositiveIntegerField(default=1)
    min_duration = models.PositiveIntegerField(default=0)
    allow_partial_match = models.BooleanField(default=False)
    acd = models.PositiveIntegerField(null=True, blank=True, help_text="Average call duration target (sec)")
    asr = models.PositiveIntegerField(null=True, blank=True, help_text="Answer seizure ratio target (%)")
    max_duration = models.PositiveIntegerField(null=True, blank=True)
    restrict_overlapping = models.BooleanField(default=False)
    concurrent_limit = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["country__name", "title"]
        unique_together = [("supplier", "title")]

    def __str__(self):
        return f"{self.title} ({self.supplier})"

    @property
    def channels(self):
        return [SMS, VOICE] if self.routing_type == self.BOTH else [self.routing_type]

    def counts(self):
        total = self.numbers.count()
        allocated = self.numbers.filter(allocations__isnull=False, allocations__ends_at__isnull=True).distinct().count()
        return total, allocated, total - allocated


class BillingGroupRate(models.Model):
    """One row per channel, payment-terms slot (1-3) and time-of-day band."""

    billing_group = models.ForeignKey(BillingGroup, on_delete=models.CASCADE, related_name="rates")
    channel = models.CharField(max_length=5, choices=CHANNELS, default=SMS)
    slot = models.PositiveSmallIntegerField(default=1, choices=[(1, "Terms 1"), (2, "Terms 2"), (3, "Terms 3")])
    payment_terms = models.ForeignKey("core.PaymentTerms", on_delete=models.PROTECT)
    time_of_day = models.ForeignKey("core.TimeOfDay", null=True, blank=True, on_delete=models.PROTECT)
    rate_in = models.DecimalField(max_digits=10, decimal_places=5, default=0,
                                  help_text="What the supplier pays us per SMS / minute")
    rate_out = models.DecimalField(max_digits=10, decimal_places=5, default=0,
                                   help_text="Default payout to the client")

    class Meta:
        ordering = ["channel", "slot"]
        unique_together = [("billing_group", "channel", "slot", "time_of_day")]


class Number(models.Model):
    number = models.CharField(max_length=20, unique=True, help_text="E.164 digits, no +")
    billing_group = models.ForeignKey(BillingGroup, on_delete=models.PROTECT, related_name="numbers")
    blocked = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["number"]

    def __str__(self):
        return "+" + self.number

    @property
    def current_allocation(self):
        return self.allocations.filter(ends_at__isnull=True).select_related("client", "payment_terms").first()

    def active_allocation(self, channel):
        return (self.allocations.filter(ends_at__isnull=True, channel=channel)
                .select_related("client", "payment_terms").first())


class Allocation(models.Model):
    """A number given to a client for one channel. History is kept: unallocating sets ends_at.
    A number in an "SMS & voice" group can have one active SMS allocation and one active voice
    allocation (normally to the same client)."""

    number = models.ForeignKey(Number, on_delete=models.CASCADE, related_name="allocations")
    channel = models.CharField(max_length=5, choices=CHANNELS, default=SMS)
    client = models.ForeignKey("crm.Account", on_delete=models.PROTECT, related_name="allocations",
                               limit_choices_to={"is_client": True})
    payment_terms = models.ForeignKey("core.PaymentTerms", null=True, blank=True, on_delete=models.PROTECT)
    all_terms = models.BooleanField(default=False)
    service = models.CharField(max_length=30, blank=True, help_text="Voice service (Route, Playback...). Blank for SMS.")
    min_duration = models.PositiveIntegerField(null=True, blank=True)
    max_duration = models.PositiveIntegerField(null=True, blank=True)
    starts_at = models.DateTimeField(default=timezone.now)
    ends_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ["-starts_at"]
        constraints = [
            models.UniqueConstraint(fields=["number", "channel"], condition=models.Q(ends_at__isnull=True),
                                    name="one_active_allocation_per_number_channel"),
        ]

    def __str__(self):
        return f"{self.number} -> {self.client} ({self.channel})"


class AllocationRate(models.Model):
    """The client's payout for this allocation, per terms slot and time band."""

    allocation = models.ForeignKey(Allocation, on_delete=models.CASCADE, related_name="rates")
    slot = models.PositiveSmallIntegerField(default=1)
    payment_terms = models.ForeignKey("core.PaymentTerms", null=True, blank=True, on_delete=models.PROTECT)
    time_of_day = models.ForeignKey("core.TimeOfDay", null=True, blank=True, on_delete=models.PROTECT)
    rate_out = models.DecimalField(max_digits=10, decimal_places=5)
