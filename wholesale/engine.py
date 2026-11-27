"""Wholesale business rules in one place: accept or refuse, route, charge, and receipts.

The SMPP server, the HTTP API and the vendor router all call these functions.
"""
import logging
import random
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Count, F, Q
from django.utils import timezone

from core.models import Country
from inventory.services import normalise
from sms.models import BlockRule
from . import pdu
from .models import BuyRate, Customer, Network, NetworkPrefix, OutboundMessage as M, RouteEntry, SellRate, Vendor, \
    VendorBind

logger = logging.getLogger("wholesale")

# SMPP command_status codes we answer customers with
ROK, RINVSRCADR, RINVDSTADR, RTHROTTLED, RSUBMITFAIL, RSYSERR = 0x00, 0x0A, 0x0B, 0x58, 0x45, 0x08
RX_NO_CREDIT = 0x45


# ---------- numbering ----------

def lookup(number):
    """(country, network) for an international number, by longest matching prefix.
    Network is None when only the country is known. Ported numbers need an HLR/MNP lookup to be
    exact; prefix data gives the network the number was first issued to."""
    prefixes = [number[:i] for i in range(min(len(number), 15), 0, -1)]
    hits = list(NetworkPrefix.objects.filter(prefix__in=prefixes).select_related("network__country"))
    if hits:
        hit = max(hits, key=lambda p: len(p.prefix))
        return hit.network.country, hit.network
    countries = [c for c in Country.objects.exclude(dial_code="") if number.startswith(c.dial_code)]
    if not countries:
        return None, None
    best = max(len(c.dial_code) for c in countries)
    countries = [c for c in countries if len(c.dial_code) == best]
    return (countries[0] if len(countries) == 1 else _shared_code(countries)), None


def _shared_code(countries):
    """+1 is shared (US, Canada, ...). Without network data, use the first by name."""
    return sorted(countries, key=lambda c: c.name)[0]


def rate_for(model, account, country, network):
    rows = model.objects.filter(account=account, country=country)
    if network:
        r = rows.filter(network=network).first()
        if r:
            return r.rate
    r = rows.filter(network__isnull=True).first()
    return r.rate if r else None


# ---------- delivery-rate stats used by routing ----------

def recent_delivery(vendor_ids, country, hours=24):
    """{vendor_id: (final messages, delivered %)} on one country, real (non-test) traffic only."""
    since = timezone.now() - timedelta(hours=hours)
    rows = (M.objects.filter(vendor_id__in=vendor_ids, country=country, created_at__gte=since, test=False,
                             status__in=M.FINAL - {M.REJECTED})
            .values("vendor_id").annotate(n=Count("pk"), ok=Count("pk", filter=Q(status=M.DELIVERED))))
    return {r["vendor_id"]: (r["n"], 100.0 * r["ok"] / r["n"]) for r in rows}


@dataclass
class Candidate:
    vendor_id: int
    buy_rate: Decimal
    priority: int = 1
    delivery: float | None = None


def candidates(customer: Customer, country, network, sell_rate, exclude=()):
    """Vendors that may carry this message, best first."""
    plan = customer.route_plan
    vendors = {v.account_id for v in Vendor.objects.filter(enabled=True)}
    with_binds = set(VendorBind.objects.filter(enabled=True).values_list("account_id", flat=True))
    allowed = (vendors & with_binds) - set(exclude)
    prio = {}
    if plan:
        entries = list(RouteEntry.objects.filter(plan=plan).filter(Q(country=country) | Q(country__isnull=True))
                       .filter(Q(network=network) | Q(network__isnull=True)))
        # most specific entries win: network > country > all countries
        for spec in ("network", "country", "any"):
            chosen = [e for e in entries if (spec == "network" and e.network_id) or
                      (spec == "country" and e.country_id and not e.network_id) or
                      (spec == "any" and not e.country_id)]
            if chosen:
                prio = {e.vendor_id: e.priority for e in chosen}
                break
        allowed &= set(prio)
    out = []
    for vid in allowed:
        buy = rate_for(BuyRate, vid, country, network)
        if buy is None:
            continue
        if not customer.allow_loss and buy >= sell_rate:
            continue
        out.append(Candidate(vid, buy, prio.get(vid, 1)))
    if not out:
        return out
    stats = recent_delivery([c.vendor_id for c in out], country)
    for c in out:
        n, pct = stats.get(c.vendor_id, (0, None))
        c.delivery = pct if n >= 50 else None
    if plan and plan.min_delivery_pct:
        kept = [c for c in out if c.delivery is None or c.delivery >= plan.min_delivery_pct]
        out = kept or out  # never strand traffic only because every vendor is under the bar
    mode = plan.mode if plan else "lcr"
    if mode == "priority":
        out.sort(key=lambda c: (c.priority, c.buy_rate))
    elif mode == "quality":
        out.sort(key=lambda c: (-(c.delivery if c.delivery is not None else -1), c.buy_rate))
    else:
        out.sort(key=lambda c: (c.buy_rate, c.priority))
    return out


# ---------- accept or refuse ----------

@dataclass
class Result:
    status: int
    message: M | None
    reason: str = ""


def _limit_hit(rule, destination, at):
    recent = M.objects.filter(destination=destination, created_at__gt=at - timedelta(hours=1)).exclude(
        status=M.REFUSED).count()
    return recent >= int(rule.value)


def check_rules(customer_account, source, destination, body, at):
    for r in BlockRule.applying(customer=customer_account):
        if r.matches(source, destination, body):
            return r
        if r.rule_type == BlockRule.LIMIT and r.value.isdigit() and _limit_hit(r, destination, at):
            return r
    return None


def accept(account, fields, smpp_user=None, api_key=None):
    """Check a customer's message and either queue it (charging the customer) or refuse it.

    Refused messages are recorded too, with the reason, so customers' support questions can be
    answered. The SMPP status in the Result is what goes back in submit_sm_resp."""
    now = timezone.now()
    test_cfg = getattr(smpp_user, "wholesale", None) if smpp_user else api_key
    is_test = bool(test_cfg and test_cfg.test_mode)
    dest = normalise(fields["destination"])
    msg = M(created_at=now, customer=account, smpp_user=smpp_user, api_key=api_key, test=is_test,
            source=fields["source"][:21], source_ton=fields.get("source_ton") or 0,
            source_npi=fields.get("source_npi") or 0, destination=dest[:21],
            esm_class=fields.get("esm_class", 0), data_coding=fields.get("data_coding", 0),
            payload=fields.get("payload", b""), body=fields.get("body", ""),
            part_ref=fields.get("part_ref"), part_num=fields.get("part_num"), part_total=fields.get("part_total"),
            registered_delivery=fields.get("registered_delivery", 0))

    def refuse(code, reason, rule=None):
        msg.status, msg.reason, msg.block_rule = M.REFUSED, reason[:200], rule
        msg.save()
        return Result(code, msg, reason)

    customer = Customer.objects.filter(account=account).first()
    if not customer or not customer.enabled or not account.active:
        return refuse(RSUBMITFAIL, "Wholesale traffic is not enabled for this account")
    if not dest.isdigit() or not 7 <= len(dest) <= 15:
        return refuse(RINVDSTADR, "Destination is not a valid international number")
    if not msg.source:
        return refuse(RINVSRCADR, "Sender ID is missing")
    if not customer.sender_allowed(msg.source):
        return refuse(RINVSRCADR, f"Sender ID {msg.source} is not on this customer's allowed list")
    rule = check_rules(account, msg.source, dest, msg.body, now)
    if rule:
        return refuse(RSUBMITFAIL, f"Blocked by rule: {rule}", rule)
    msg.country, msg.network = lookup(dest)
    if not msg.country:
        return refuse(RINVDSTADR, "Unknown country for this number")
    msg.charge_on = customer.charge_on

    if is_test:
        msg.sell_rate = test_cfg.test_price
    else:
        sell = rate_for(SellRate, account, msg.country, msg.network)
        if sell is None:
            return refuse(RINVDSTADR, f"No rate for {msg.network or msg.country} on this customer")
        msg.sell_rate = sell
        if not candidates(customer, msg.country, msg.network, sell):
            return refuse(RSUBMITFAIL, f"No route to {msg.network or msg.country}")

    with transaction.atomic():
        c = Customer.objects.select_for_update().get(pk=customer.pk)
        price = msg.sell_rate
        if price and c.balance + c.credit_limit < price:
            return refuse(RX_NO_CREDIT, "Not enough credit")
        # Hold the price now so a burst can't run past the credit limit; refunded if it never goes out.
        if price:
            Customer.objects.filter(pk=c.pk).update(balance=F("balance") - price)
        msg.charged = price
        msg.status = M.QUEUED
        msg.next_attempt_at = now
        msg.save()
    return Result(ROK, msg)


# ---------- money ----------

def refund(msg: M, why=""):
    if msg.charged:
        Customer.objects.filter(account_id=msg.customer_id).update(balance=F("balance") + msg.charged)
        msg.charged = Decimal(0)


def settle(msg: M):
    """Apply the customer's billing basis once a message has its final status."""
    if msg.charge_on == Customer.DELIVERED and msg.status != M.DELIVERED:
        refund(msg)
    elif msg.charge_on == Customer.DELIVERED and msg.status == M.DELIVERED and not msg.charged:
        # e.g. a late real DELIVRD after we had marked it UNKNOWN and refunded
        Customer.objects.filter(account_id=msg.customer_id).update(balance=F("balance") - msg.sell_rate)
        msg.charged = msg.sell_rate


# ---------- vendor results ----------

def vendor_accepted(msg_id, vendor_id, bind_id, vendor_message_id, buy_rate):
    raw = (vendor_message_id or "").strip().lower()
    M.objects.filter(pk=msg_id).update(
        status=M.SUBMITTED, vendor_id=vendor_id, vendor_bind_id=bind_id, vendor_message_id=raw[:65],
        vendor_message_id_alt=pdu.id_alternative(raw)[:65], buy_rate=buy_rate, submitted_at=timezone.now(),
        next_attempt_at=None)


def vendor_failed(msg_id, vendor_id, bind_id, error, retry_same=False):
    """The vendor refused it (or never answered). Try the next vendor, or reject it honestly."""
    with transaction.atomic():
        msg = M.objects.select_for_update().get(pk=msg_id)
        msg.tried = msg.tried + [{"vendor": vendor_id, "bind": bind_id, "error": error[:120],
                                  "at": timezone.now().isoformat(timespec="seconds"), "retry": retry_same}]
        failures = [t for t in msg.tried if not t.get("retry")]
        if len(failures) >= settings.WHOLESALE_MAX_VENDOR_ATTEMPTS:
            return reject(msg, f"Rejected by {len(failures)} vendors, last: {error}")
        msg.next_attempt_at = timezone.now() + timedelta(seconds=1 if retry_same else 0)
        msg.save(update_fields=["tried", "next_attempt_at"])


def reject(msg: M, reason, stat="REJECTD"):
    """We accepted it but could not get it to any vendor. The receipt says exactly that."""
    msg.status, msg.reason, msg.stat, msg.err = M.REJECTED, reason[:200], stat, ""
    msg.done_at = timezone.now()
    msg.next_attempt_at = None
    refund(msg)
    _queue_dlr(msg)
    msg.save()


def tried_vendors(msg: M):
    return [t["vendor"] for t in msg.tried if not t.get("retry")]


# ---------- receipts from vendors ----------

STAT_TO_STATUS = {"DELIVRD": M.DELIVERED, "UNDELIV": M.UNDELIVERED, "EXPIRED": M.EXPIRED,
                  "DELETED": M.UNDELIVERED, "REJECTD": M.REJECTED, "UNKNOWN": M.UNKNOWN}


def find_by_vendor_id(vendor_id, dlr_id):
    ids = pdu.id_candidates(dlr_id)
    since = timezone.now() - timedelta(days=7)
    return (M.objects.filter(vendor_id=vendor_id, created_at__gte=since)
            .filter(Q(vendor_message_id__in=ids) | Q(vendor_message_id_alt__in=ids))
            .order_by("-created_at").first())


def count_unmatched(bind_id, dlr_id):
    VendorBind.objects.filter(pk=bind_id).update(unmatched_dlrs=F("unmatched_dlrs") + 1)
    logger.warning("Receipt for unknown vendor id %s on vendor bind %s", dlr_id, bind_id)


def vendor_receipt(vendor_id, bind_id, receipt):
    """Store a vendor's receipt exactly as reported and queue it for the customer.
    Returns the message, or None if the ID matched nothing (the caller retries briefly, since a
    receipt can arrive before the vendor's submit_sm_resp has been saved).

    Status only moves forward: the first final receipt stands. Repeats and later contradicting
    receipts are kept in dlr_raw for support but change nothing. The one exception is our own
    "no receipt in time" UNKNOWN, which a late real receipt replaces."""
    with transaction.atomic():
        msg = find_by_vendor_id(vendor_id, receipt["id"])
        if not msg:
            return None
        msg = M.objects.select_for_update().get(pk=msg.pk)
        stat = receipt["stat"]
        msg.dlr_raw, msg.dlr_received_at = receipt["raw"], timezone.now()
        already_final = msg.status in M.FINAL and not msg.no_receipt
        if stat not in pdu.FINAL_STATS or already_final:  # intermediate (ENROUTE/ACCEPTD) or repeat
            msg.save(update_fields=["dlr_raw", "dlr_received_at"])
            return msg
        msg.stat, msg.err, msg.network_error = stat, receipt["err"], receipt["network_error"]
        msg.done_at = receipt["done_at"] or msg.dlr_received_at
        msg.status = STAT_TO_STATUS[stat]
        msg.no_receipt = False
        settle(msg)
        _queue_dlr(msg)
        msg.save()
        return msg


def expire_unanswered(now=None):
    """Messages with a vendor and no receipt after the timeout: UNKNOWN, and we say why.
    A real receipt arriving later still replaces this."""
    now = now or timezone.now()
    cutoff = now - timedelta(hours=settings.WHOLESALE_DLR_TIMEOUT_HOURS)
    n = 0
    for msg in M.objects.filter(status=M.SUBMITTED, submitted_at__lt=cutoff)[:1000]:
        msg.status, msg.stat, msg.err, msg.no_receipt = M.UNKNOWN, "UNKNOWN", "", True
        msg.reason = f"No receipt from route within {settings.WHOLESALE_DLR_TIMEOUT_HOURS}h"
        msg.done_at = now
        settle(msg)
        _queue_dlr(msg)
        msg.save()
        n += 1
    # Content is personal data: keep the text only as long as needed, keep the metadata.
    keep = now - timedelta(days=settings.WHOLESALE_BODY_RETENTION_DAYS)
    M.objects.filter(created_at__lt=keep).exclude(body="").update(body="", payload=b"")
    qcut = now - timedelta(seconds=settings.WHOLESALE_QUEUE_TIMEOUT)
    for msg in M.objects.filter(status=M.QUEUED, test=False, created_at__lt=qcut)[:1000]:
        reject(msg, "No vendor could take it in time", stat="UNDELIV")
        n += 1
    return n


# ---------- test binds ----------

def simulate_test(msg: M, rng=random):
    """Test bind: never sent anywhere. The receipt is simulated with the bind's delivered % and is
    marked test in our records and in the receipt's error field ("TST")."""
    cfg = msg.smpp_user.wholesale if msg.smpp_user_id else msg.api_key
    pct = cfg.test_delivered_pct if cfg else 100
    stat = "DELIVRD" if rng.random() * 100 < pct else "UNDELIV"
    msg.status, msg.stat, msg.err = STAT_TO_STATUS[stat], stat, "TST"
    msg.reason = "Test bind: simulated receipt, not sent to any network"
    msg.submitted_at = msg.done_at = timezone.now()
    msg.next_attempt_at = None
    settle(msg)
    _queue_dlr(msg)
    msg.save()


# ---------- receipts to customers ----------

def _queue_dlr(msg: M):
    msg.dlr_state = M.DLR_PENDING if msg.wants_dlr() else M.DLR_NONE
    msg.dlr_attempts = 0
    msg.dlr_error = ""


def due_dlrs(account_id, limit=50):
    """Receipts ready to go out to a customer over SMPP (picked by the SMPP server)."""
    now = timezone.now()
    qs = (M.objects.filter(customer_id=account_id, api_key__isnull=True,
                           dlr_attempts__lt=settings.WHOLESALE_DLR_MAX_ATTEMPTS)
          .filter(Q(dlr_state=M.DLR_PENDING) | Q(dlr_state=M.DLR_FAILED) |
                  Q(dlr_state=M.DLR_SENDING, dlr_last_attempt__lt=now - timedelta(seconds=60)))
          .order_by("done_at")[:limit])
    out = []
    for m in qs:
        if m.dlr_state == M.DLR_FAILED and (m.dlr_last_attempt or now) + timedelta(
                seconds=30 * 2 ** m.dlr_attempts) > now:
            continue
        m.dlr_state, m.dlr_attempts, m.dlr_last_attempt = M.DLR_SENDING, m.dlr_attempts + 1, now
        m.save(update_fields=["dlr_state", "dlr_attempts", "dlr_last_attempt"])
        out.append(m)
    return out


def dlr_result(msg_pk, ok, error=""):
    M.objects.filter(pk=msg_pk).update(dlr_state=M.DLR_SENT if ok else M.DLR_FAILED, dlr_error=error[:200])


def push_http_dlrs(limit=200):
    """POST receipts to customers' HTTP callback URLs. Called by the forward_pending worker."""
    import requests
    now = timezone.now()
    qs = (M.objects.filter(api_key__isnull=False, dlr_attempts__lt=settings.WHOLESALE_DLR_MAX_ATTEMPTS)
          .filter(Q(dlr_state=M.DLR_PENDING) | Q(dlr_state=M.DLR_FAILED)).select_related("api_key")
          .order_by("done_at")[:limit])
    done = 0
    for m in qs:
        if m.dlr_state == M.DLR_FAILED and (m.dlr_last_attempt or now) + timedelta(
                seconds=30 * 2 ** m.dlr_attempts) > now:
            continue
        data = {"id": m.message_id, "status": m.stat, "err": m.err, "to": m.destination, "from": m.source,
                "done_at": m.done_at.isoformat() if m.done_at else "", "test": "1" if m.test else "0"}
        m.dlr_attempts += 1
        m.dlr_last_attempt = now
        try:
            r = requests.post(m.api_key.dlr_url, data=data, timeout=settings.FORWARD_TIMEOUT)
            ok, err = 200 <= r.status_code < 300, f"HTTP {r.status_code}"
        except requests.RequestException as e:
            ok, err = False, f"{type(e).__name__}: {e}"
        m.dlr_state, m.dlr_error = (M.DLR_SENT, "") if ok else (M.DLR_FAILED, err[:200])
        m.save(update_fields=["dlr_state", "dlr_error", "dlr_attempts", "dlr_last_attempt"])
        done += 1
    return done
