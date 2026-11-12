"""Inbound SMS: record, route to the allocated client, and forward to their HTTP endpoint.

Jasmin receives the SMS from the supplier (over the supplier's SMPP bind) and posts it to
/ingest/jasmin/. `receive()` stores it and works out the client; `forward()` pushes it to
the client's endpoint. `manage.py forward_pending` retries failures.
"""
import logging

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from crm.models import SmppUser, Smsc
from inventory.models import Allocation, Number
from inventory.services import normalise
from .models import BlockRule, Message

logger = logging.getLogger(__name__)


def _rates_for(allocation, number):
    """Pick the rate row: allocation terms if set, else the first slot."""
    group = number.billing_group
    group_rates = list(group.rates.filter(channel="sms").select_related("payment_terms"))
    alloc_rates = list(allocation.rates.select_related("payment_terms")) if allocation else []
    terms = allocation.payment_terms if allocation else None

    def pick(rows):
        if not rows:
            return None
        if terms:
            for r in rows:
                if r.payment_terms_id == terms.pk:
                    return r
        return rows[0]

    gr, ar = pick(group_rates), pick(alloc_rates)
    rate_in = gr.rate_in if gr else 0
    rate_out = ar.rate_out if ar else (gr.rate_out if gr else 0)
    terms_label = (terms or (gr.payment_terms if gr else None))
    return rate_in, rate_out, str(terms_label or "")


@transaction.atomic
def receive(*, source, destination, body, origin_connector="", smpp_username="", received_at=None,
            source_ton=None, source_npi=None, dest_ton=None, dest_npi=None, registered_delivery=False):
    dest = normalise(destination)
    msg = Message(received_at=received_at or timezone.now(), source=source[:32], destination=dest,
                  body=body, source_ton=source_ton, source_npi=source_npi, dest_ton=dest_ton,
                  dest_npi=dest_npi, registered_delivery=registered_delivery)

    # Which supplier bind did it arrive on?
    if smpp_username:
        msg.smpp_user = SmppUser.objects.filter(username=smpp_username).select_related("account").first()
        msg.supplier = msg.smpp_user.account if msg.smpp_user else None
    if not msg.supplier and origin_connector.startswith("smsc") and origin_connector[4:].isdigit():
        msg.smsc = Smsc.objects.filter(pk=int(origin_connector[4:])).select_related("account").first()
        msg.supplier = msg.smsc.account if msg.smsc else None

    number = Number.objects.select_related("billing_group__supplier", "billing_group__currency").filter(number=dest).first()
    if number:
        msg.number = number
        msg.billing_group = number.billing_group
        msg.supplier = msg.supplier or number.billing_group.supplier
        msg.currency = number.billing_group.currency.code
        allocation = (Allocation.objects.filter(number=number, channel="sms", ends_at__isnull=True)
                      .select_related("client", "payment_terms").first())
        msg.rate_in, msg.rate_out, msg.payment_terms = _rates_for(allocation, number)
        if number.billing_group.routing_type == "voice":
            allocation = None  # voice-only numbers don't route SMS
        if allocation and not number.blocked:
            msg.client = allocation.client
            msg.http_endpoint = allocation.client.http_endpoints.filter(enabled=True).first()

    rule = BlockRule.blocks(msg.source, dest, msg.client)
    if rule or (number and number.blocked):
        msg.status = Message.BLOCKED
        msg.last_error = f"Blocked by rule: {rule}" if rule else "Number is blocked"
        msg.rate_out = 0
    elif not msg.client:
        msg.status = Message.NO_ROUTE
        msg.last_error = "Number not found" if not number else "Number not allocated"
        msg.rate_out = 0
    elif not msg.http_endpoint:
        msg.status = Message.FAILED
        msg.last_error = "Client has no enabled HTTP endpoint"
    msg.save()
    if number:
        number.billing_group.__class__.objects.filter(pk=number.billing_group_id).update(last_used=msg.received_at)
    return msg


def forward(msg: Message) -> bool:
    ep = msg.http_endpoint
    if not ep:
        return False
    params = {ep.to_param: msg.destination, ep.from_param: msg.source,
              ep.message_param: msg.body, ep.uuid_param: str(msg.uuid)}
    msg.forward_attempts += 1
    msg.last_attempt_at = timezone.now()
    try:
        if ep.method == "GET":
            r = requests.get(ep.url, params=params, timeout=settings.FORWARD_TIMEOUT)
        else:
            r = requests.post(ep.url, data=params, timeout=settings.FORWARD_TIMEOUT)
        ok = 200 <= r.status_code < 300
        msg.last_error = "" if ok else f"HTTP {r.status_code}: {r.text[:200]}"
    except requests.RequestException as e:
        ok = False
        msg.last_error = f"{type(e).__name__}: {e}"[:300]
    msg.status = Message.FORWARDED if ok else Message.FAILED
    if ok:
        msg.forwarded_at = timezone.now()
    msg.save(update_fields=["status", "forward_attempts", "forwarded_at", "last_error", "last_attempt_at"])
    return ok
