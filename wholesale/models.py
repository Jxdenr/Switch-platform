"""Wholesale SMS (A2P MT): customers send us traffic, we route it to vendors and pass back
the vendors' delivery receipts (DLRs).

Words used here, chosen to match the wholesale industry:
  * Wholesale customer: binds to us and sends traffic (submit_sm or HTTP). Pays us the sell rate.
  * Vendor: terminates traffic (ideally with direct network links). We bind to them and pay the buy rate.

Rules that never bend:
  * A DLR status is only ever what the vendor sent us. If no receipt arrives in time the message
    is marked UNKNOWN ("no receipt from route"), never DELIVRD.
  * Test binds never touch a network. Their simulated receipts are labelled test everywhere.
"""
import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models

from core.tenancy import TenantOwned


def new_id():
    return uuid.uuid4().hex


class Network(models.Model):
    """A mobile network (MCC/MNC) with the number prefixes that belong to it."""

    country = models.ForeignKey("core.Country", on_delete=models.PROTECT, related_name="networks")
    name = models.CharField(max_length=80)
    mcc = models.CharField("MCC", max_length=3)
    mnc = models.CharField("MNC", max_length=3)

    class Meta:
        ordering = ["country__name", "name"]
        unique_together = [("mcc", "mnc")]

    def __str__(self):
        return f"{self.name} ({self.mcc}-{self.mnc})"


class NetworkPrefix(models.Model):
    network = models.ForeignKey(Network, on_delete=models.CASCADE, related_name="prefixes")
    prefix = models.CharField(max_length=15, unique=True, help_text="International format, e.g. 447700")

    class Meta:
        ordering = ["prefix"]
        verbose_name_plural = "network prefixes"

    def __str__(self):
        return self.prefix


class RoutePlan(TenantOwned):
    """A named set of routes, e.g. "Premium" or "Standard". Each customer uses one plan.

    Least cost: cheapest vendor first. Priority: the order you set. Best delivery: the vendor
    with the highest real delivery rate on that country in the last 24 hours first."""

    LCR, PRIORITY, QUALITY = "lcr", "priority", "quality"
    MODES = [(LCR, "Least cost"), (PRIORITY, "Priority order"), (QUALITY, "Best delivery rate")]

    title = models.CharField(max_length=60)
    mode = models.CharField(max_length=8, choices=MODES, default=LCR)
    min_delivery_pct = models.PositiveSmallIntegerField(
        "Skip vendors delivering under (%)", null=True, blank=True,
        help_text="Real delivery rate over the last 24 hours on that country, once a vendor has sent 50+ "
                  "messages there. Blank = never skip.")
    notes = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["title"]
        unique_together = [("tenant", "title")]

    def __str__(self):
        return self.title


class RouteEntry(TenantOwned):
    """One vendor allowed for a destination in a plan. Blank country = every country."""

    plan = models.ForeignKey(RoutePlan, on_delete=models.CASCADE, related_name="entries")
    country = models.ForeignKey("core.Country", null=True, blank=True, on_delete=models.CASCADE)
    network = models.ForeignKey(Network, null=True, blank=True, on_delete=models.CASCADE,
                                help_text="Blank = all networks in the country")
    vendor = models.ForeignKey("crm.Account", on_delete=models.CASCADE, related_name="route_entries")
    priority = models.PositiveSmallIntegerField(default=1, help_text="1 is tried first (priority mode)")

    class Meta:
        ordering = ["plan", "country__name", "priority"]
        verbose_name_plural = "route entries"

    def __str__(self):
        return f"{self.plan}: {self.country or 'All countries'} -> {self.vendor} ({self.priority})"


class Customer(TenantOwned):
    """Wholesale settings for an account that sends us traffic."""

    ACCEPTED, DELIVERED = "accepted", "delivered"
    CHARGE_ON = [(ACCEPTED, "When a vendor accepts it"), (DELIVERED, "Only when delivered (DELIVRD)")]

    account = models.OneToOneField("crm.Account", on_delete=models.CASCADE, related_name="wholesale_customer")
    enabled = models.BooleanField(default=True)
    route_plan = models.ForeignKey(RoutePlan, null=True, blank=True, on_delete=models.SET_NULL,
                                   help_text="Blank = every vendor with a rate, cheapest first")
    charge_on = models.CharField("Charge", max_length=9, choices=CHARGE_ON, default=ACCEPTED)
    balance = models.DecimalField(max_digits=14, decimal_places=5, default=0, editable=False)
    credit_limit = models.DecimalField(
        max_digits=14, decimal_places=2, default=0,
        help_text="0 = prepaid: traffic stops when the balance runs out. Above 0 = they may owe up to this much.")
    allow_loss = models.BooleanField("Allow routing at a loss", default=False,
                                     help_text="Off: a vendor is only used if it costs less than this customer's rate.")
    allowed_senders = models.CharField(
        max_length=500, blank=True,
        help_text="Comma separated sender IDs this customer may use. Blank = any (blocking rules still apply).")
    low_balance_alert = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)

    class Meta:
        verbose_name = "wholesale customer"

    def __str__(self):
        return str(self.account)

    @property
    def available(self):
        return self.balance + self.credit_limit

    def sender_allowed(self, sender):
        allowed = [s.strip().lower() for s in self.allowed_senders.split(",") if s.strip()]
        return not allowed or sender.lower() in allowed


class LedgerEntry(TenantOwned):
    """Top-ups, payments and manual adjustments to a customer's balance. Per-message charges are
    stored on each message instead."""

    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="ledger")
    at = models.DateTimeField(auto_now_add=True)
    amount = models.DecimalField(max_digits=14, decimal_places=5, help_text="Positive adds credit")
    note = models.CharField(max_length=200, blank=True)
    user = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ["-at"]


class Vendor(TenantOwned):
    account = models.OneToOneField("crm.Account", on_delete=models.CASCADE, related_name="wholesale_vendor")
    enabled = models.BooleanField(default=True)
    route_type = models.CharField(max_length=20, blank=True,
                                  help_text="What they claim, e.g. Direct, Premium, SIM. Verify with live tests.")
    notes = models.CharField(max_length=300, blank=True)

    def __str__(self):
        return str(self.account)


class VendorBind(TenantOwned):
    """We bind out to a vendor's SMSC (we are the SMPP client) and send traffic over it."""

    TRX, TX_RX = "trx", "tx_rx"
    MODES = [(TRX, "Transceiver (one connection)"), (TX_RX, "Transmitter + receiver (two connections)")]

    account = models.ForeignKey("crm.Account", on_delete=models.CASCADE, related_name="vendor_binds")
    title = models.CharField(max_length=80)
    host = models.CharField(max_length=120)
    port = models.PositiveIntegerField(default=2775)
    username = models.CharField("System ID", max_length=16)
    password = models.CharField(max_length=9)
    system_type = models.CharField(max_length=13, blank=True)
    bind_mode = models.CharField(max_length=5, choices=MODES, default=TRX)
    tps = models.PositiveSmallIntegerField("Max messages per second", default=10)
    window = models.PositiveSmallIntegerField(default=10, help_text="Messages sent before waiting for replies")
    src_ton = models.PositiveSmallIntegerField(null=True, blank=True, help_text="Override source TON. Blank = auto")
    src_npi = models.PositiveSmallIntegerField(null=True, blank=True)
    enabled = models.BooleanField(default=True)
    bound = models.BooleanField(default=False, editable=False)
    last_status_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_error = models.CharField(max_length=200, blank=True, editable=False)
    unmatched_dlrs = models.PositiveIntegerField(default=0, editable=False,
                                                 help_text="Receipts whose ID matched none of our messages")

    class Meta:
        verbose_name = "vendor bind"

    def __str__(self):
        return f"{self.title} ({self.host}:{self.port})"


class TestSettings(TenantOwned):
    """Abstract: settings shared by both kinds of customer bind."""

    tps = models.PositiveSmallIntegerField("Max messages per second", default=20)
    test_mode = models.BooleanField(
        default=False, help_text="Test bind: nothing is sent to any network. Receipts are simulated and labelled test.")
    test_delivered_pct = models.PositiveSmallIntegerField("Test: % delivered", default=100)
    test_price = models.DecimalField("Test: price per message", max_digits=10, decimal_places=5, default=0)

    class Meta:
        abstract = True


class ApiKey(TestSettings):
    """A wholesale customer sends traffic over HTTP: POST /api/wholesale/send/."""

    account = models.ForeignKey("crm.Account", on_delete=models.CASCADE, related_name="wholesale_keys")
    title = models.CharField(max_length=80)
    key = models.CharField(max_length=64, unique=True, default=new_id)
    ip = models.CharField(max_length=200, blank=True, help_text="Allowed source IPs, comma separated. Blank = any.")
    dlr_url = models.URLField("DLR callback URL", max_length=400, blank=True,
                              help_text="We POST each delivery receipt here. Blank = no receipts.")
    enabled = models.BooleanField(default=True)
    last_used = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        verbose_name = "wholesale HTTP key"

    def __str__(self):
        return self.title

    def allows(self, ip):
        allowed = [x.strip() for x in self.ip.split(",") if x.strip()]
        return not allowed or ip in allowed


class SmppTestSettings(TestSettings):
    """Throughput and test-mode settings for a wholesale customer's SMPP user."""

    smpp_user = models.OneToOneField("crm.SmppUser", on_delete=models.CASCADE, related_name="wholesale")

    class Meta:
        verbose_name = "wholesale SMPP settings"


class Rate(TenantOwned):
    """Price per message for a country, or one network in it. Network beats country."""

    country = models.ForeignKey("core.Country", on_delete=models.CASCADE, related_name="+")
    network = models.ForeignKey(Network, null=True, blank=True, on_delete=models.CASCADE, related_name="+",
                                help_text="Blank = every network in the country")
    rate = models.DecimalField(max_digits=10, decimal_places=5)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
        ordering = ["country__name", "network__name"]

    def __str__(self):
        return f"{self.network or self.country}: {self.rate}"


class SellRate(Rate):
    account = models.ForeignKey("crm.Account", on_delete=models.CASCADE, related_name="sell_rates")

    class Meta(Rate.Meta):
        unique_together = [("account", "country", "network")]


class BuyRate(Rate):
    account = models.ForeignKey("crm.Account", on_delete=models.CASCADE, related_name="buy_rates")

    class Meta(Rate.Meta):
        unique_together = [("account", "country", "network")]


class OutboundMessage(TenantOwned):
    """One wholesale message part. Long messages are one record per part, each with its own
    message ID and receipt, which is how SMPP works end to end."""

    REFUSED, QUEUED, SUBMITTED = "refused", "queued", "submitted"
    DELIVERED, UNDELIVERED, EXPIRED, REJECTED, UNKNOWN = "delivered", "undelivered", "expired", "rejected", "unknown"
    STATUSES = [(REFUSED, "Refused by us"), (QUEUED, "Queued"), (SUBMITTED, "With vendor, waiting for receipt"),
                (DELIVERED, "Delivered"), (UNDELIVERED, "Undelivered"), (EXPIRED, "Expired"),
                (REJECTED, "Rejected"), (UNKNOWN, "Unknown")]
    FINAL = {DELIVERED, UNDELIVERED, EXPIRED, REJECTED, UNKNOWN}

    # Receipt we owe the customer
    DLR_NONE, DLR_PENDING, DLR_SENDING, DLR_SENT, DLR_FAILED = "none", "pending", "sending", "sent", "failed"
    DLR_STATES = [(DLR_NONE, "Not requested"), (DLR_PENDING, "To send"), (DLR_SENDING, "Sending"),
                  (DLR_SENT, "Sent"), (DLR_FAILED, "Retrying")]

    message_id = models.CharField(max_length=32, unique=True, default=new_id,
                                  help_text="Our ID, given to the customer in submit_sm_resp")
    created_at = models.DateTimeField(db_index=True)
    customer = models.ForeignKey("crm.Account", on_delete=models.PROTECT, related_name="wholesale_sent")
    smpp_user = models.ForeignKey("crm.SmppUser", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    api_key = models.ForeignKey(ApiKey, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    test = models.BooleanField(default=False, db_index=True, help_text="Test bind: never sent to a network")

    source = models.CharField(max_length=21)
    source_ton = models.PositiveSmallIntegerField(default=0)
    source_npi = models.PositiveSmallIntegerField(default=0)
    destination = models.CharField(max_length=21, db_index=True)
    esm_class = models.PositiveSmallIntegerField(default=0)
    data_coding = models.PositiveSmallIntegerField(default=0)
    payload = models.BinaryField(help_text="short_message exactly as the customer sent it (passed on unchanged)")
    body = models.TextField(blank=True, help_text="Decoded text, for filters and display")
    part_ref = models.PositiveIntegerField(null=True, blank=True)
    part_num = models.PositiveSmallIntegerField(null=True, blank=True)
    part_total = models.PositiveSmallIntegerField(null=True, blank=True)
    registered_delivery = models.PositiveSmallIntegerField(default=0)

    country = models.ForeignKey("core.Country", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    network = models.ForeignKey(Network, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    status = models.CharField(max_length=11, choices=STATUSES, default=QUEUED, db_index=True)
    reason = models.CharField(max_length=200, blank=True, help_text="Why we refused or rejected it")
    block_rule = models.ForeignKey("sms.BlockRule", null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="wholesale_hits")
    vendor = models.ForeignKey("crm.Account", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    vendor_bind = models.ForeignKey(VendorBind, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    vendor_message_id = models.CharField(max_length=65, blank=True, db_index=True)
    vendor_message_id_alt = models.CharField(max_length=65, blank=True, db_index=True,
                                             help_text="Same ID in the other base (hex/decimal)")
    tried = models.JSONField(default=list, blank=True, help_text="Vendor attempts: [{vendor, bind, error, at}]")
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)

    # The receipt, exactly as the vendor reported it
    stat = models.CharField(max_length=7, blank=True, help_text="DELIVRD, UNDELIV, EXPIRED, REJECTD, ...")
    err = models.CharField(max_length=10, blank=True)
    network_error = models.CharField(max_length=12, blank=True)
    done_at = models.DateTimeField(null=True, blank=True, help_text="Done date from the vendor's receipt")
    dlr_received_at = models.DateTimeField(null=True, blank=True)
    dlr_raw = models.CharField(max_length=300, blank=True)
    no_receipt = models.BooleanField(default=False, help_text="No receipt came from the vendor in time")

    # Money (wholesale currency). charged is what this message currently costs the customer.
    sell_rate = models.DecimalField(max_digits=10, decimal_places=5, default=0)
    buy_rate = models.DecimalField(max_digits=10, decimal_places=5, default=0)
    charged = models.DecimalField(max_digits=10, decimal_places=5, default=0)
    charge_on = models.CharField(max_length=9, default=Customer.ACCEPTED)

    dlr_state = models.CharField(max_length=7, choices=DLR_STATES, default=DLR_NONE, db_index=True)
    dlr_attempts = models.PositiveSmallIntegerField(default=0)
    dlr_last_attempt = models.DateTimeField(null=True, blank=True)
    dlr_error = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["customer", "created_at"]), models.Index(fields=["vendor", "created_at"]),
                   models.Index(fields=["status", "next_attempt_at"])]

    def __str__(self):
        return f"{self.message_id} {self.source} -> {self.destination}"

    @property
    def cost(self):
        return self.buy_rate if self.vendor_id and self.status not in (self.REJECTED, self.QUEUED, self.REFUSED) \
            else Decimal(0)

    @property
    def via(self):
        return "HTTP" if self.api_key_id else "SMPP"

    def wants_dlr(self):
        """SMPP registered_delivery bits 0-1: 1 = always, 2 = only on failure. HTTP: callback set."""
        if self.api_key_id:
            return bool(self.api_key and self.api_key.dlr_url)
        mode = self.registered_delivery & 3
        if mode == 1:
            return True
        return mode == 2 and self.status != self.DELIVERED


def wholesale_currency():
    return getattr(settings, "WHOLESALE_CURRENCY", settings.DEFAULT_CURRENCY)
