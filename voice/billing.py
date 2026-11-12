"""Turn a raw CDR into a routed, billed Call."""
import math
from datetime import datetime, timezone as dt_tz
from decimal import Decimal

from django.db import transaction

from inventory.models import Allocation, Number
from inventory.services import normalise
from .models import Call


def billable_seconds(billsec, first, nxt, minimum=0):
    """Billing increments like '60/60' or '1/1': the first block is charged in full, then
    each further block of `nxt` seconds. Unanswered calls bill 0."""
    if billsec <= 0:
        return 0
    first, nxt = max(first or 1, 1), max(nxt or 1, 1)
    secs = first if billsec <= first else first + math.ceil((billsec - first) / nxt) * nxt
    return max(secs, minimum or 0)


def _pick(rows, terms):
    if not rows:
        return None
    if terms:
        for r in rows:
            if r.payment_terms_id == terms.pk:
                return r
    return rows[0]


def _ts(v):
    """FreeSWITCH gives epoch microseconds (`*_uepoch`) or seconds (`*_epoch`)."""
    if v in (None, "", "0", 0):
        return None
    v = int(v)
    if v > 10**14:
        v = v / 1_000_000
    return datetime.fromtimestamp(v, tz=dt_tz.utc)


@transaction.atomic
def record_call(*, uuid, number_called, caller_id="", start=None, answer=None, end=None, duration=0, billsec=0,
                **extra):
    dest = normalise(number_called)
    call = Call(uuid=uuid, number_called=dest, caller_id=caller_id[:64], start_stamp=start, answer_stamp=answer,
                end_stamp=end, duration=duration, billsec=billsec,
                **{k: v for k, v in extra.items() if v not in (None, "")})
    number = Number.objects.select_related("billing_group__supplier", "billing_group__currency").filter(number=dest).first()
    if number:
        g = number.billing_group
        call.number, call.billing_group, call.supplier, call.currency = number, g, g.supplier, g.currency.code
        alloc = (Allocation.objects.filter(number=number, channel="voice", ends_at__isnull=True)
                 .select_related("client", "payment_terms").first())
        if alloc and not number.blocked and g.routing_type in ("voice", "both"):
            call.client, call.service = alloc.client, alloc.service
        terms = alloc.payment_terms if alloc else None
        gr = _pick(list(g.rates.filter(channel="voice")), terms)
        ar = _pick(list(alloc.rates.all()), terms) if alloc else None
        call.rate_in = gr.rate_in if gr else Decimal(0)
        call.rate_out = (ar.rate_out if ar else (gr.rate_out if gr else Decimal(0))) if call.client else Decimal(0)
        call.payment_terms = str(terms or (gr.payment_terms if gr else ""))
        # The allocation's min/max duration (if set) overrides the billing group's.
        min_d = (alloc.min_duration if alloc and alloc.min_duration is not None else None) or g.min_duration
        max_d = (alloc.max_duration if alloc and alloc.max_duration else None) or g.max_duration
        call.billable_seconds = billable_seconds(billsec, g.billing_increment_first, g.billing_increment_next, min_d)
        if max_d:
            call.billable_seconds = min(call.billable_seconds, max_d)
        minutes = Decimal(call.billable_seconds) / 60
        call.due_in = (call.rate_in * minutes).quantize(Decimal("0.00001"))
        call.due_out = (call.rate_out * minutes).quantize(Decimal("0.00001"))
    call.save()
    return call


def from_freeswitch_json(cdr: dict):
    """Map a FreeSWITCH mod_json_cdr document to record_call() arguments."""
    v = cdr.get("variables", {})
    cf = (cdr.get("callflow") or [{}])
    cf = cf[0] if isinstance(cf, list) else cf
    profile = cf.get("caller_profile", {})

    def i(x):
        try:
            return int(x)
        except (TypeError, ValueError):
            return None

    mos = v.get("rtp_audio_in_mos")
    return dict(
        uuid=v.get("uuid") or cdr.get("core-uuid", ""),
        number_called=v.get("destination_number") or profile.get("destination_number", ""),
        caller_id=v.get("caller_id_number") or profile.get("caller_id_number", ""),
        cnam=v.get("caller_id_name") or profile.get("caller_id_name", ""),
        start=_ts(v.get("start_uepoch") or v.get("start_epoch")),
        answer=_ts(v.get("answer_uepoch") or v.get("answer_epoch")),
        end=_ts(v.get("end_uepoch") or v.get("end_epoch")),
        duration=i(v.get("duration")) or 0,
        billsec=i(v.get("billsec")) or 0,
        pdd_ms=i(v.get("progress_mediamsec")),
        from_ip=v.get("sip_network_ip", ""),
        remote_media_ip=v.get("remote_media_ip", ""),
        hangup_cause=v.get("hangup_cause", ""),
        hangup_cause_q850=i(v.get("hangup_cause_q850")),
        proto_specific_cause=v.get("proto_specific_hangup_cause", ""),
        sip_hangup_disposition=v.get("sip_hangup_disposition", ""),
        mos_in=Decimal(mos) if mos else None,
    )
