"""Numbers behind the dashboard: headline totals with change vs the previous period,
a traffic chart, top suppliers and clients, and the things that need attention."""
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate, TruncHour
from django.utils import timezone

from sms.models import Message
from voice.models import Call

PERIODS = [("today", "Today"), ("7d", "Last 7 days"), ("30d", "Last 30 days"), ("month", "This month"),
           ("lastmonth", "Last month")]
CHANNELS = [("all", "SMS & voice"), ("sms", "SMS"), ("voice", "Voice")]
ZERO = Decimal(0)


def period_bounds(key):
    tz = timezone.get_current_timezone()
    today = timezone.localdate()
    day = lambda d: timezone.make_aware(datetime.combine(d, time.min), tz)  # noqa: E731
    now = timezone.now()
    if key == "today":
        start, end = day(today), now
    elif key == "30d":
        start, end = day(today - timedelta(days=29)), now
    elif key == "month":
        start, end = day(today.replace(day=1)), now
    elif key == "lastmonth":
        first_this = today.replace(day=1)
        start, end = day((first_this - timedelta(days=1)).replace(day=1)), day(first_this)
    else:
        start, end = day(today - timedelta(days=6)), now
    prev = (start - (end - start), start)
    return start, end, prev


def _sms(start, end, client=None, currency=None):
    qs = Message.objects.filter(received_at__gte=start, received_at__lt=end).exclude(
        status__in=[Message.NO_ROUTE, Message.BLOCKED])
    if currency:
        qs = qs.filter(currency=currency)
    return qs.filter(client=client) if client else qs


def _calls(start, end, client=None, currency=None):
    qs = Call.objects.filter(start_stamp__gte=start, start_stamp__lt=end, client__isnull=False)
    if currency:
        qs = qs.filter(currency=currency)
    return qs.filter(client=client) if client else qs


def currencies(start, end, channel, client=None):
    """[(code, due_in, due_out)] for every currency with traffic, biggest first. Amounts in different
    currencies are never added together; the dashboard headlines the biggest and lists the rest."""
    acc = {}
    amount = "rate_out" if client else "rate_in"
    if channel in ("all", "sms"):
        for r in _sms(start, end, client).values("currency").annotate(i=Sum("rate_in"), o=Sum("rate_out")):
            x = acc.setdefault(r["currency"] or "", [ZERO, ZERO])
            x[0] += r["i"] or ZERO
            x[1] += r["o"] or ZERO
    if channel in ("all", "voice"):
        for r in _calls(start, end, client).values("currency").annotate(i=Sum("due_in"), o=Sum("due_out")):
            x = acc.setdefault(r["currency"] or "", [ZERO, ZERO])
            x[0] += r["i"] or ZERO
            x[1] += r["o"] or ZERO
    key = (lambda kv: kv[1][1]) if amount == "rate_out" else (lambda kv: kv[1][0])
    return [(k, i, o) for k, (i, o) in sorted(acc.items(), key=key, reverse=True)]


def main_currency(start, end, channel, client=None):
    from django.conf import settings
    rows = currencies(start, end, channel, client)
    return rows[0][0] if rows else getattr(settings, "DEFAULT_CURRENCY", "GBP")


def delivery(start, end, channel, client=None):
    """How much routed traffic actually reached the client: SMS delivered %, voice ASR and ACD."""
    d = {}
    if channel in ("all", "sms"):
        a = _sms(start, end, client).aggregate(n=Count("pk"), ok=Count("pk", filter=Q(status=Message.FORWARDED)),
                                               bad=Count("pk", filter=Q(status=Message.FAILED)))
        d["sms_total"], d["sms_ok"], d["sms_failed"] = a["n"], a["ok"], a["bad"]
        d["sms_pct"] = (a["ok"] / a["n"] * 100) if a["n"] else None
    if channel in ("all", "voice"):
        a = _calls(start, end, client).aggregate(n=Count("pk"), ans=Count("pk", filter=Q(answer_stamp__isnull=False)),
                                                 dur=Sum("duration", filter=Q(answer_stamp__isnull=False)))
        d["calls"], d["answered"] = a["n"], a["ans"]
        d["asr"] = (a["ans"] / a["n"] * 100) if a["n"] else None
        d["acd"] = ((a["dur"] or 0) / a["ans"]) if a["ans"] else None
    return d


def unrouted(start, end, channel):
    """Traffic that arrived for numbers with no client, or was blocked: money left on the table."""
    u = {"sms": 0, "blocked": 0, "calls": 0}
    if channel in ("all", "sms"):
        a = Message.objects.filter(received_at__gte=start, received_at__lt=end).aggregate(
            n=Count("pk", filter=Q(status=Message.NO_ROUTE)), b=Count("pk", filter=Q(status=Message.BLOCKED)))
        u["sms"], u["blocked"] = a["n"], a["b"]
    if channel in ("all", "voice"):
        u["calls"] = Call.objects.filter(start_stamp__gte=start, start_stamp__lt=end, client__isnull=True).count()
    u["total"] = u["sms"] + u["calls"]
    return u


def totals(start, end, channel, client=None, currency=None):
    t = {"sms": 0, "minutes": ZERO, "calls": 0, "due_in": ZERO, "due_out": ZERO}
    if channel in ("all", "sms"):
        a = _sms(start, end, client, currency).aggregate(n=Count("pk"), i=Sum("rate_in"), o=Sum("rate_out"))
        t["sms"] = a["n"]
        t["due_in"] += a["i"] or ZERO
        t["due_out"] += a["o"] or ZERO
    if channel in ("all", "voice"):
        a = _calls(start, end, client, currency).aggregate(n=Count("pk"), s=Sum("billable_seconds"), i=Sum("due_in"),
                                                 o=Sum("due_out"))
        t["calls"] = a["n"]
        t["minutes"] = Decimal(a["s"] or 0) / 60
        t["due_in"] += a["i"] or ZERO
        t["due_out"] += a["o"] or ZERO
    t["margin"] = t["due_in"] - t["due_out"]
    t["margin_pct"] = (t["margin"] / t["due_in"] * 100) if t["due_in"] else ZERO
    return t


def change(now, before):
    if not before:
        return None
    return float((Decimal(now) - Decimal(before)) / Decimal(before) * 100)


def series(start, end, channel, hourly, client=None, currency=None):
    """[(bucket start, due_in, due_out)] with empty buckets filled in."""
    trunc_s = TruncHour("received_at") if hourly else TruncDate("received_at")
    trunc_c = TruncHour("start_stamp") if hourly else TruncDate("start_stamp")
    acc = {}
    if channel in ("all", "sms"):
        for r in _sms(start, end, client, currency).annotate(b=trunc_s).values("b").annotate(i=Sum("rate_in"),
                                                                                    o=Sum("rate_out")):
            x = acc.setdefault(_key(r["b"], hourly), [ZERO, ZERO])
            x[0] += r["i"] or ZERO
            x[1] += r["o"] or ZERO
    if channel in ("all", "voice"):
        for r in _calls(start, end, client, currency).annotate(b=trunc_c).values("b").annotate(i=Sum("due_in"),
                                                                                      o=Sum("due_out")):
            x = acc.setdefault(_key(r["b"], hourly), [ZERO, ZERO])
            x[0] += r["i"] or ZERO
            x[1] += r["o"] or ZERO
    out, cur = [], timezone.localtime(start)
    step = timedelta(hours=1) if hourly else timedelta(days=1)
    while cur < end:
        k = _key(cur, hourly)
        i, o = acc.get(k, [ZERO, ZERO])
        out.append((k, i, o))
        cur += step
    return out


def _key(v, hourly):
    if isinstance(v, datetime):
        v = timezone.localtime(v) if timezone.is_aware(v) else v
        return v.replace(minute=0, second=0, microsecond=0, tzinfo=None) if hourly else v.date()
    return v


def chart(points, width=640, height=190, pad_b=22):
    """SVG geometry for a bar chart: bar = due in, darker part = margin kept."""
    if not points:
        return None
    top = max((float(i) for _, i, _ in points), default=0) or 1
    n = len(points)
    slot = width / n
    bw = min(max(2.0, slot * 0.7), 40.0)
    plot_h = height - pad_b
    bars = []
    every = max(1, n // 7)
    for idx, (k, i, o) in enumerate(points):
        x = idx * slot + (slot - bw) / 2
        h_in = float(i) / top * (plot_h - 8)
        h_m = max(0.0, float(i - o)) / top * (plot_h - 8)
        label = k.strftime("%H:%M") if isinstance(k, datetime) else k.strftime("%d %b")
        bars.append({"x": round(x, 1), "w": round(bw, 1), "y_in": round(plot_h - h_in, 1), "h_in": round(h_in, 1),
                     "y_m": round(plot_h - h_m, 1), "h_m": round(h_m, 1), "label": label,
                     "show_label": idx % every == 0, "lx": round(x + bw / 2, 1), "due_in": i, "margin": i - o})
    return {"bars": bars, "w": width, "h": height, "base": plot_h, "top": top, "ly": height - 6}


def top_accounts(start, end, channel, field, currency=None):
    """Top 5 suppliers (field='supplier', by due in) or clients (field='client', by due out)."""
    rows = {}
    amount_s, amount_c = ("rate_in", "due_in") if field == "supplier" else ("rate_out", "due_out")
    if channel in ("all", "sms"):
        for r in _sms(start, end, currency=currency).exclude(**{f"{field}__isnull": True}).values(f"{field}_id", f"{field}__title") \
                .annotate(n=Count("pk"), a=Sum(amount_s)):
            x = rows.setdefault(r[f"{field}_id"], {"id": r[f"{field}_id"], "title": r[f"{field}__title"], "sms": 0,
                                                   "minutes": ZERO, "amount": ZERO})
            x["sms"] += r["n"]
            x["amount"] += r["a"] or ZERO
    if channel in ("all", "voice"):
        for r in _calls(start, end, currency=currency).exclude(**{f"{field}__isnull": True}).values(f"{field}_id", f"{field}__title") \
                .annotate(s=Sum("billable_seconds"), a=Sum(amount_c)):
            x = rows.setdefault(r[f"{field}_id"], {"id": r[f"{field}_id"], "title": r[f"{field}__title"], "sms": 0,
                                                   "minutes": ZERO, "amount": ZERO})
            x["minutes"] += Decimal(r["s"] or 0) / 60
            x["amount"] += r["a"] or ZERO
    out = sorted(rows.values(), key=lambda x: x["amount"], reverse=True)[:5]
    biggest = out[0]["amount"] if out and out[0]["amount"] else 1
    for x in out:
        x["share"] = float(x["amount"] / biggest * 100)
    return out
