"""Volumes, statements and invoices per account.

Supplier side: traffic the supplier sent to our numbers. They pay us (Due In) -> Invoice.
Client side:   traffic we passed to the client. We pay them (Due Out) -> Statement.
Both cover SMS and voice. Amounts come from the rates copied onto each SMS and call when it
arrived, so a document for a past month never changes when rates are edited later.
"""
import calendar
import csv
import io
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from email.utils import formataddr, parseaddr

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.db.models import Count, F, Q, Sum
from django.db.models.functions import TruncDate, TruncMonth
from django.template.loader import render_to_string
from django.utils import timezone

from sms.models import Message
from voice.models import Call
from .models import Document

SUPPLIER, CLIENT = "supplier", "client"
BREAKDOWNS = [("month", "Month"), ("day", "Day"), ("group", "Billing group"), ("country", "Country"),
              ("number", "Number")]
ZERO = Decimal(0)


def month_bounds(month: str | date | None = None):
    """'2026-10' -> (first day, last day) as dates. None = last full month."""
    if month is None:
        first_this = timezone.localdate().replace(day=1)
        d = (first_this - timedelta(days=1)).replace(day=1)
    elif isinstance(month, date):
        d = month.replace(day=1)
    else:
        d = datetime.strptime(month, "%Y-%m").date()
    return d, d.replace(day=calendar.monthrange(d.year, d.month)[1])


def aware_range(first: date, last: date):
    tz = timezone.get_current_timezone()
    return (timezone.make_aware(datetime.combine(first, time.min), tz),
            timezone.make_aware(datetime.combine(last, time.max), tz))


def _sms(account, side, start, end):
    qs = Message.objects.filter(received_at__range=(start, end))
    if side == SUPPLIER:
        return qs.filter(supplier=account, number__isnull=False), "rate_in"
    return qs.filter(client=account).exclude(status__in=[Message.BLOCKED, Message.NO_ROUTE]), "rate_out"


def _calls(account, side, start, end):
    qs = Call.objects.filter(start_stamp__range=(start, end))
    if side == SUPPLIER:
        return qs.filter(supplier=account, number__isnull=False), "due_in"
    return qs.filter(client=account), "due_out"


SMS_KEYS = {"month": TruncMonth("received_at"), "day": TruncDate("received_at"), "group": F("billing_group__title"),
            "country": F("billing_group__country__name"), "number": F("destination")}
CALL_KEYS = {"month": TruncMonth("start_stamp"), "day": TruncDate("start_stamp"), "group": F("billing_group__title"),
             "country": F("billing_group__country__name"), "number": F("number_called")}


def volumes(account, side, start, end, channel="all", by="month"):
    """Rows of {key, sms, sms_amount, calls, connected, minutes, voice_amount, total, currency}."""
    rows = {}

    def row(key, cur):
        return rows.setdefault((key, cur), {"key": key, "currency": cur, "sms": 0, "sms_amount": ZERO, "calls": 0,
                                            "connected": 0, "secs": 0, "voice_amount": ZERO})

    if channel in ("all", "sms"):
        qs, amount = _sms(account, side, start, end)
        for r in qs.annotate(k=SMS_KEYS[by]).values("k", "currency").annotate(n=Count("pk"), amt=Sum(amount)):
            x = row(r["k"], r["currency"])
            x["sms"] += r["n"]
            x["sms_amount"] += r["amt"] or ZERO
    if channel in ("all", "voice"):
        qs, amount = _calls(account, side, start, end)
        for r in qs.annotate(k=CALL_KEYS[by]).values("k", "currency").annotate(
                n=Count("pk"), c=Count("pk", filter=Q(billsec__gt=0)), s=Sum("billable_seconds"), amt=Sum(amount)):
            x = row(r["k"], r["currency"])
            x["calls"] += r["n"]
            x["connected"] += r["c"]
            x["secs"] += r["s"] or 0
            x["voice_amount"] += r["amt"] or ZERO
    out = []
    for x in rows.values():
        if isinstance(x["key"], datetime):
            x["key"] = timezone.localtime(x["key"]).date() if timezone.is_aware(x["key"]) else x["key"].date()
        x["minutes"] = Decimal(x["secs"]) / 60
        x["total"] = x["sms_amount"] + x["voice_amount"]
        out.append(x)
    out.sort(key=lambda x: (str(x["key"] or ""), x["currency"]))
    return out


def totals(rows):
    t = {"sms": 0, "sms_amount": ZERO, "calls": 0, "connected": 0, "minutes": ZERO, "voice_amount": ZERO,
         "total": ZERO}
    for r in rows:
        for k in t:
            t[k] += r[k]
    t["currencies"] = sorted({r["currency"] for r in rows if r["currency"]})
    return t


def rows_csv(rows, by_label):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([by_label, "Currency", "SMS", "SMS amount", "Calls", "Connected", "Billable minutes",
                "Voice amount", "Total"])
    for r in rows:
        w.writerow([r["key"] if r["key"] is not None else "(none)", r["currency"], r["sms"], f'{r["sms_amount"]:.5f}',
                    r["calls"], r["connected"], f'{r["minutes"]:.2f}', f'{r["voice_amount"]:.5f}',
                    f'{r["total"]:.5f}'])
    return buf.getvalue()


def sides(account):
    return [s for s, ok in ((SUPPLIER, account.is_supplier), (CLIENT, account.is_client)) if ok]


def kind_for(side):
    return Document.INVOICE if side == SUPPLIER else Document.STATEMENT


def side_for(kind):
    return SUPPLIER if kind == Document.INVOICE else CLIENT


def document_context(account, kind, first, last, doc=None):
    start, end = aware_range(first, last)
    side = side_for(kind)
    daily = volumes(account, side, start, end, by="day")
    by_group = volumes(account, side, start, end, by="group")
    t = totals(daily)
    number = doc.number if doc else doc_number(account, kind, first)
    return {"acc": account, "kind": kind, "kind_label": dict(Document.KINDS)[kind], "number": number,
            "first": first, "last": last, "daily": daily, "by_group": by_group, "t": t, "doc": doc,
            "currency": (t["currencies"][0] if len(t["currencies"]) == 1 else ", ".join(t["currencies"]))
            or (account.currency.code if account.currency else ""),
            # Each portal's own company and bank details, never another portal's.
            "company_name": account.tenant.legal_name, "company_details": account.tenant.company_details,
            "address": account.addresses.first(), "issued": doc.created_at.date() if doc else timezone.localdate()}


def doc_number(account, kind, first):
    return f'{"INV" if kind == Document.INVOICE else "ST"}-{first:%Y%m}-{account.pk:04d}'


@transaction.atomic
def create_document(account, kind, first):
    first, last = month_bounds(first)
    doc = Document.objects.filter(account=account, kind=kind, period_start=first).first()
    ctx = document_context(account, kind, first, last, doc)
    t = ctx["t"]
    values = dict(period_end=last, currency=ctx["currency"][:3], sms_count=t["sms"], sms_amount=t["sms_amount"],
                  calls=t["calls"], minutes=t["minutes"].quantize(Decimal("0.01")),
                  voice_amount=t["voice_amount"], total=t["total"].quantize(Decimal("0.01")))
    if doc:
        if not doc.sent_at:  # an unsent draft is refreshed; a sent one is never changed
            Document.objects.filter(pk=doc.pk).update(**values)
            doc.refresh_from_db()
    else:
        doc = Document.objects.create(account=account, kind=kind, period_start=first,
                                      number=doc_number(account, kind, first), **values)
    return doc


def recipients(account):
    return [e.strip() for e in account.billing_email.replace(";", ",").split(",") if e.strip()]


def send_document(doc, to=None):
    """Email the document (HTML body + CSV of daily volumes). Returns True when sent."""
    to = to or recipients(doc.account)
    if not to:
        doc.last_error = "No billing email on the account"
        doc.save(update_fields=["last_error"])
        return False
    ctx = document_context(doc.account, doc.kind, doc.period_start, doc.period_end, doc)
    html = render_to_string("crm/document.html", {**ctx, "email": True})
    text = render_to_string("crm/document_email.txt", ctx)
    tenant = doc.account.tenant
    subject = f'{ctx["kind_label"]} {doc.number} from {tenant.legal_name} ({doc.period_start:%B %Y})'
    # Sent from the platform's address (so SPF/DKIM pass) under the portal's name; replies go to the portal.
    sender = formataddr((tenant.legal_name, parseaddr(settings.DEFAULT_FROM_EMAIL)[1]))
    msg = EmailMultiAlternatives(subject, text, sender, to,
                                 reply_to=[tenant.billing_email] if tenant.billing_email else None)
    msg.attach_alternative(html, "text/html")
    msg.attach(f"{doc.number}-daily.csv", rows_csv(ctx["daily"], "Day"), "text/csv")
    try:
        msg.send()
    except Exception as e:  # SMTP errors vary by server; record and let the next run retry
        doc.last_error = f"{type(e).__name__}: {e}"[:300]
        doc.save(update_fields=["last_error"])
        return False
    doc.sent_at, doc.sent_to, doc.last_error = timezone.now(), ", ".join(to)[:300], ""
    doc.save(update_fields=["sent_at", "sent_to", "last_error"])
    return True
