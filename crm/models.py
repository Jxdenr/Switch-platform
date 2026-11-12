"""CRM: suppliers and clients, and every way they connect to the switch.

One Account can be a supplier, a client or both, as in the original CRM.
"""
from django.db import models


class Account(models.Model):
    title = models.CharField(max_length=120, unique=True)
    is_supplier = models.BooleanField(default=False)
    is_client = models.BooleanField(default=False)
    tags = models.CharField(max_length=200, blank=True)
    account_manager = models.ForeignKey(
        "auth.User", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="managed_accounts", help_text="Called 'Delegate' on the old platform.",
    )
    currency = models.ForeignKey("core.Currency", null=True, blank=True, on_delete=models.PROTECT)
    payment_terms = models.ForeignKey("core.PaymentTerms", null=True, blank=True, on_delete=models.PROTECT)
    process_insights = models.BooleanField(default=True)
    active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_access = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["title"]

    def __str__(self):
        return self.title

    @property
    def kind(self):
        if self.is_supplier and self.is_client:
            return "Supplier & client"
        return "Supplier" if self.is_supplier else ("Client" if self.is_client else "Contact")


class Address(models.Model):
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="addresses")
    title = models.CharField(max_length=60, default="Head office")
    address1 = models.CharField(max_length=120)
    address2 = models.CharField(max_length=120, blank=True)
    address3 = models.CharField(max_length=120, blank=True)
    postcode = models.CharField(max_length=20, blank=True)
    country = models.ForeignKey("core.Country", null=True, blank=True, on_delete=models.SET_NULL)

    def __str__(self):
        return f"{self.title}: {self.address1}"


class Contact(models.Model):
    TITLES = [(t, t) for t in ["Mr", "Mrs", "Miss", "Ms", "Dr", "Prof"]]
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="contacts")
    title = models.CharField(max_length=5, choices=TITLES, blank=True)
    first_name = models.CharField(max_length=60)
    surname = models.CharField(max_length=60, blank=True)
    email = models.EmailField(blank=True)
    telephone = models.CharField(max_length=40, blank=True)
    skype = models.CharField(max_length=80, blank=True)
    telegram = models.CharField(max_length=80, blank=True)
    whatsapp = models.CharField(max_length=40, blank=True)

    def __str__(self):
        return f"{self.first_name} {self.surname}".strip()


class BankAccount(models.Model):
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="bank_accounts")
    title = models.CharField(max_length=60)
    bank_name = models.CharField(max_length=100, blank=True)
    account_name = models.CharField(max_length=100, blank=True)
    iban = models.CharField(max_length=40, blank=True)
    swift = models.CharField(max_length=15, blank=True)
    account_number = models.CharField(max_length=30, blank=True)
    sort_code = models.CharField(max_length=10, blank=True)
    currency = models.ForeignKey("core.Currency", null=True, blank=True, on_delete=models.SET_NULL)

    def __str__(self):
        return self.title


class SmppUser(models.Model):
    """'SMS User': the supplier binds TO US. We are the SMPP server."""

    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="smpp_users")
    title = models.CharField(max_length=80)
    ip = models.CharField(max_length=200, blank=True, help_text="Allowed source IPs, comma separated. Blank = any.")
    username = models.CharField(max_length=16, unique=True, help_text="SMPP system_id the supplier binds with")
    password = models.CharField(max_length=9, help_text="SMPP passwords are max 8-9 chars")
    system_type = models.CharField(max_length=13, blank=True, help_text="Called 'System ID' on the old platform")
    dlr_wait_time = models.PositiveIntegerField(default=0, help_text="Seconds to wait for a client DLR")
    enabled = models.BooleanField(default=True)
    bound = models.BooleanField(default=False, editable=False)
    last_status_at = models.DateTimeField(null=True, blank=True, editable=False)

    def __str__(self):
        return f"{self.title} ({self.username})"


class Smsc(models.Model):
    """'SMSc': WE bind to the supplier's SMSC. We are the SMPP client."""

    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="smscs")
    title = models.CharField(max_length=80)
    ip = models.CharField(max_length=120)
    port = models.PositiveIntegerField(default=2775)
    username = models.CharField(max_length=16)
    password = models.CharField(max_length=9)
    system_type = models.CharField(max_length=13, blank=True)
    src_ton = models.PositiveSmallIntegerField(null=True, blank=True)
    dst_ton = models.PositiveSmallIntegerField(null=True, blank=True)
    transceiver = models.BooleanField(default=True)
    routing_attrs = models.CharField(max_length=200, blank=True)
    enforce_dlr = models.BooleanField(default=False)
    enabled = models.BooleanField(default=True)
    bound = models.BooleanField(default=False, editable=False)
    last_status_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        verbose_name = "SMSC"

    def __str__(self):
        return f"{self.title} ({self.ip}:{self.port})"

    @property
    def connector_id(self):
        return f"smsc{self.pk}"


class Prefix(models.Model):
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="prefixes")
    prefix = models.CharField(max_length=20)

    class Meta:
        verbose_name_plural = "prefixes"

    def __str__(self):
        return self.prefix


class HttpEndpoint(models.Model):
    """'SMS HTTP': where we push inbound SMS for a client."""

    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="http_endpoints")
    title = models.CharField(max_length=80)
    url = models.URLField(max_length=400)
    method = models.CharField(max_length=4, choices=[("GET", "GET"), ("POST", "POST")], default="POST")
    to_param = models.CharField(max_length=40, default="to")
    from_param = models.CharField(max_length=40, default="from")
    message_param = models.CharField(max_length=40, default="message")
    uuid_param = models.CharField(max_length=40, default="uuid")
    enabled = models.BooleanField(default=True)

    def __str__(self):
        return self.title


class Notification(models.Model):
    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="notifications")
    url = models.URLField(max_length=400)

    def __str__(self):
        return self.url


class OriginationIp(models.Model):
    """Voice: IPs calls will come from."""

    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="origination_ips")
    title = models.CharField(max_length=80)
    ip = models.GenericIPAddressField()
    ip_type = models.CharField(max_length=5, choices=[("B", "B-Num"), ("A", "A-Num")], default="B")

    def __str__(self):
        return f"{self.title} {self.ip}"


class TerminationIp(models.Model):
    """Voice: IPs we send calls to, in fail-over order."""

    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="termination_ips")
    title = models.CharField(max_length=80)
    ip = models.CharField(max_length=120)
    priority = models.PositiveSmallIntegerField(default=1)

    class Meta:
        ordering = ["priority"]

    def __str__(self):
        return f"{self.title} {self.ip}"
