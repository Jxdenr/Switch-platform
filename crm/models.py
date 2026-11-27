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
    billing_email = models.CharField(
        max_length=300, blank=True,
        help_text="Where monthly statements and invoices go. Separate several addresses with commas.")
    auto_statements = models.BooleanField(
        default=False, help_text="Email last month's invoice (suppliers) or statement (clients) on the 1st.")
    created_at = models.DateTimeField(auto_now_add=True)
    last_access = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["title"]

    def __str__(self):
        return self.title

    @property
    def list_url(self):
        return "/suppliers/" if self.is_supplier or not self.is_client else "/clients/"

    @property
    def list_label(self):
        return "Suppliers" if self.is_supplier or not self.is_client else "Clients"

    @property
    def nav_key(self):
        return "suppliers" if self.is_supplier or not self.is_client else "clients"

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
    """Someone binds TO US over SMPP. We are the SMPP server.

    A supplier binds to send us SMS for our numbers (submit_sm). A client binds to receive
    the SMS for their allocated numbers (we send them deliver_sm)."""

    SUPPLIER, CLIENT, WHOLESALE = "supplier", "client", "wholesale"
    ROLES = [(SUPPLIER, "Supplier sends SMS to us"), (CLIENT, "Client receives SMS from us"),
             (WHOLESALE, "Wholesale customer sends traffic and gets receipts")]

    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="smpp_users")
    role = models.CharField(max_length=9, choices=ROLES, default=SUPPLIER)
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


def _new_key():
    import secrets
    return secrets.token_urlsafe(24)


class HttpUser(models.Model):
    """A supplier sends us SMS over HTTP instead of SMPP (the HTTP 'bind').

    They call /api/sms/inbound/ with this key and to, from and message."""

    account = models.ForeignKey(Account, on_delete=models.CASCADE, related_name="http_users")
    title = models.CharField(max_length=80)
    api_key = models.CharField(max_length=64, unique=True, default=_new_key)
    ip = models.CharField(max_length=200, blank=True, help_text="Allowed source IPs, comma separated. Blank = any.")
    enabled = models.BooleanField(default=True)
    last_used = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        verbose_name = "HTTP user"

    def __str__(self):
        return self.title

    def allows(self, ip):
        allowed = [x.strip() for x in self.ip.split(",") if x.strip()]
        return not allowed or ip in allowed


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


class Document(models.Model):
    """A monthly invoice (to a supplier, for traffic they sent us) or statement (to a client,
    for traffic we passed to them). Totals are frozen when the document is created."""

    INVOICE, STATEMENT = "invoice", "statement"
    KINDS = [(INVOICE, "Invoice"), (STATEMENT, "Statement")]

    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="documents")
    kind = models.CharField(max_length=9, choices=KINDS)
    number = models.CharField(max_length=30, unique=True)
    period_start = models.DateField()
    period_end = models.DateField()
    currency = models.CharField(max_length=3, blank=True)
    sms_count = models.PositiveIntegerField(default=0)
    sms_amount = models.DecimalField(max_digits=14, decimal_places=5, default=0)
    calls = models.PositiveIntegerField(default=0)
    minutes = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    voice_amount = models.DecimalField(max_digits=14, decimal_places=5, default=0)
    total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    sent_to = models.CharField(max_length=300, blank=True)
    last_error = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-period_start", "kind"]
        unique_together = [("account", "kind", "period_start")]

    def __str__(self):
        return f"{self.get_kind_display()} {self.number}"
