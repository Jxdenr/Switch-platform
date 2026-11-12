"""Call detail records (CDRs). One row per inbound call to one of our numbers.

Fields follow the old platform's CDR screen (FreeSWITCH names), plus the routing and
billing we work out when the CDR arrives.
"""
from django.db import models


class Call(models.Model):
    uuid = models.CharField(max_length=64, unique=True)
    start_stamp = models.DateTimeField(db_index=True)
    answer_stamp = models.DateTimeField(null=True, blank=True)
    end_stamp = models.DateTimeField(null=True, blank=True)
    duration = models.PositiveIntegerField(default=0, help_text="Seconds from start to end")
    billsec = models.PositiveIntegerField(default=0, help_text="Answered seconds (from the switch)")
    billable_seconds = models.PositiveIntegerField(default=0, help_text="After increments and minimum duration")
    pdd_ms = models.PositiveIntegerField(null=True, blank=True, help_text="Post-dial delay")
    number_called = models.CharField(max_length=32, db_index=True)
    caller_id = models.CharField(max_length=64, blank=True)
    cnam = models.CharField(max_length=64, blank=True)
    from_ip = models.CharField(max_length=64, blank=True)
    remote_media_ip = models.CharField(max_length=64, blank=True)
    hangup_cause = models.CharField(max_length=40, blank=True)
    hangup_cause_q850 = models.PositiveSmallIntegerField(null=True, blank=True)
    proto_specific_cause = models.CharField(max_length=40, blank=True)
    sip_hangup_disposition = models.CharField(max_length=40, blank=True)
    mos_in = models.DecimalField(max_digits=4, decimal_places=2, null=True, blank=True)
    # routing
    number = models.ForeignKey("inventory.Number", null=True, blank=True, on_delete=models.SET_NULL)
    billing_group = models.ForeignKey("inventory.BillingGroup", null=True, blank=True, on_delete=models.SET_NULL)
    supplier = models.ForeignKey("crm.Account", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    client = models.ForeignKey("crm.Account", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    service = models.CharField(max_length=30, blank=True)
    # billing, copied in when the CDR arrives (per minute rates)
    currency = models.CharField(max_length=3, blank=True)
    payment_terms = models.CharField(max_length=30, blank=True)
    rate_in = models.DecimalField(max_digits=10, decimal_places=5, default=0)
    rate_out = models.DecimalField(max_digits=10, decimal_places=5, default=0)
    due_in = models.DecimalField(max_digits=12, decimal_places=5, default=0)
    due_out = models.DecimalField(max_digits=12, decimal_places=5, default=0)

    class Meta:
        ordering = ["-start_stamp"]
        indexes = [models.Index(fields=["client", "start_stamp"]), models.Index(fields=["supplier", "start_stamp"])]

    def __str__(self):
        return f"{self.caller_id} -> {self.number_called}"

    @property
    def connected(self):
        return self.billsec > 0
