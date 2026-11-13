import csv
import hmac
from datetime import datetime, time, timedelta

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, F, Q, Sum
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from core.access import client_account, staff_required
from core.models import log
from crm.models import Account, HttpEndpoint, SmppUser, Smsc
from . import routing
from .models import BlockRule, Message

COLUMNS = [  # key, label, default on
    ("received_at", "Date", True), ("supplier", "Supplier", True), ("client", "Client", True),
    ("source", "Source", True), ("destination", "Destination", True),
    ("registered_delivery", "Registered delivery", False), ("status", "Status", True),
    ("dest_ton", "Dest TON", False), ("source_ton", "Source TON", False), ("dest_npi", "Dest NPI", False),
    ("source_npi", "Source NPI", False), ("billing_group", "Billing group", False),
    ("rate_in", "Rate in", False), ("rate_out", "Rate out", False), ("body", "Message", True),
]


def _date_range(f):
    """'2026-08-01 00:00 - 2026-08-31 23:59' (same format as the old platform) or from/to fields."""
    tz = timezone.get_current_timezone()
    raw = f.get("date_range", "")
    start = end = None
    if " - " in raw:
        a, b = raw.split(" - ", 1)
        try:
            start = timezone.make_aware(datetime.strptime(a.strip(), "%Y-%m-%d %H:%M"), tz)
            end = timezone.make_aware(datetime.strptime(b.strip(), "%Y-%m-%d %H:%M"), tz) + timedelta(seconds=59)
        except ValueError:
            pass
    if not start:
        today = timezone.localdate()
        start = timezone.make_aware(datetime.combine(today - timedelta(days=6), time.min), tz)
        end = timezone.make_aware(datetime.combine(today, time.max), tz)
    return start, end


def _filtered(request):
    f = request.GET
    start, end = _date_range(f)
    qs = Message.objects.filter(received_at__range=(start, end)).select_related("supplier", "client", "billing_group")
    acc = client_account(request.user)
    if acc:
        qs = qs.filter(client=acc)
    for key, field in [("supplier", "supplier_id"), ("client", "client_id"), ("smpp_bind", "smpp_user_id"),
                       ("smsc", "smsc_id"), ("http_bind", "http_endpoint_id"), ("status", "status")]:
        if f.get(key):
            qs = qs.filter(**{field: f[key]})
    if f.get("source"):
        qs = qs.filter(source__icontains=f["source"])
    if f.get("destination"):
        qs = qs.filter(destination__contains=f["destination"].lstrip("+"))
    if f.get("text"):
        qs = qs.filter(body__icontains=f["text"])
    return qs, start, end


@login_required
def records(request):
    qs, start, end = _filtered(request)
    cols = request.GET.getlist("col") or [k for k, _, on in COLUMNS if on]
    if request.GET.get("export") == "csv":
        resp = HttpResponse(content_type="text/csv")
        resp["Content-Disposition"] = f'attachment; filename="sms-{start:%Y%m%d}-{end:%Y%m%d}.csv"'
        w = csv.writer(resp)
        w.writerow([lbl for k, lbl, _ in COLUMNS if k in cols])
        for m in qs.iterator(chunk_size=5000):
            w.writerow([getattr(m, k) if k not in ("supplier", "client", "billing_group") else (getattr(m, k) or "")
                        for k, _, _ in COLUMNS if k in cols])
        log(request.user, "sms.export", f"{start:%Y-%m-%d}..{end:%Y-%m-%d}")
        return resp
    totals = qs.aggregate(n=Count("pk"), due_in=Sum("rate_in"), due_out=Sum("rate_out"),
                          failed=Count("pk", filter=Q(status=Message.FAILED)))
    page = Paginator(qs, int(request.GET.get("per", 50))).get_page(request.GET.get("page"))
    is_client = bool(client_account(request.user))
    return render(request, "sms/records.html", {
        "page": page, "f": request.GET, "nav": "records", "totals": totals, "columns": COLUMNS, "cols": cols,
        "date_range": f"{start.astimezone():%Y-%m-%d %H:%M} - {end.astimezone():%Y-%m-%d %H:%M}",
        "is_client": is_client,
        "suppliers": Account.objects.filter(is_supplier=True) if not is_client else [],
        "clients": Account.objects.filter(is_client=True) if not is_client else [],
        "smpp_users": SmppUser.objects.all() if not is_client else [],
        "endpoints": HttpEndpoint.objects.all() if not is_client else [],
        "statuses": Message.STATUSES,
    })


@login_required
def ticker(request):
    if request.GET.get("format") == "json":
        qs = Message.objects.select_related("client").order_by("-received_at")
        acc = client_account(request.user)
        if acc:
            qs = qs.filter(client=acc)
        if request.GET.get("after"):
            qs = qs.filter(pk__gt=int(request.GET["after"]))
        rows = [{"id": m.pk, "at": timezone.localtime(m.received_at).strftime("%H:%M:%S"),
                 "client": str(m.client or "—"), "source": m.source, "destination": m.destination,
                 "body": m.body[:160], "status": m.get_status_display()} for m in qs[:50]]
        return JsonResponse({"rows": rows})
    return render(request, "sms/ticker.html", {"nav": "ticker"})


@staff_required
def stats(request):
    qs, start, end = _filtered(request)
    by = request.GET.get("by", "supplier_client")
    if by not in ("supplier_client", "supplier", "client", "group", "day", "bind"):
        by = "supplier_client"
    group_fields = {"supplier_client": ["supplier__title", "client__title", "payment_terms", "currency"],
                    "supplier": ["supplier__title", "currency"], "client": ["client__title", "currency"],
                    "day": ["received_at__date", "currency"],
                    "bind": ["supplier__title", "smpp_user__title", "smsc__title", "currency"]}[by]
    rows = (qs.exclude(status__in=[Message.NO_ROUTE, Message.BLOCKED]).values(*group_fields)
            .annotate(count=Count("pk"), due_in=Sum("rate_in"), due_out=Sum("rate_out"))
            .annotate(margin=F("due_in") - F("due_out")).order_by(*group_fields))
    for r in rows:
        r["pct"] = (r["margin"] / r["due_in"] * 100) if r["due_in"] else 0
    return render(request, "sms/stats.html", {
        "rows": rows, "by": by, "f": request.GET, "nav": "stats", "group_fields": group_fields,
        "date_range": f"{start.astimezone():%Y-%m-%d %H:%M} - {end.astimezone():%Y-%m-%d %H:%M}"})


@staff_required
@require_POST
def resend(request, pk):
    m = get_object_or_404(Message, pk=pk)
    ok = routing.forward(m)
    log(request.user, "sms.resend", m.uuid, ok=ok)
    (messages.success if ok else messages.error)(request, "Forwarded." if ok else f"Still failing: {m.last_error}")
    return redirect(request.POST.get("back") or "sms_records")


class BlockRuleForm(forms.ModelForm):
    class Meta:
        model = BlockRule
        fields = ["rule_type", "value", "account", "reason"]


@staff_required
def block_rules(request):
    form = BlockRuleForm(request.POST or None)
    if request.method == "POST":
        if request.POST.get("delete"):
            BlockRule.objects.filter(pk=request.POST["delete"]).delete()
            messages.success(request, "Rule removed.")
            return redirect("block_rules")
        if form.is_valid():
            r = form.save()
            log(request.user, "block_rule.add", r)
            messages.success(request, f"Added rule: {r}.")
            return redirect("block_rules")
    return render(request, "sms/block_rules.html", {"form": form, "rules": BlockRule.objects.select_related("account"),
                                                    "nav": "blocking"})


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


@csrf_exempt
@require_POST
def ingest_jasmin(request):
    """Jasmin HTTP client connector posts every inbound (MO) SMS here.

    Jasmin sends form fields: id, from, to, origin-connector, priority, coding, validity,
    content (or binary). We add `token` (shared secret) and, for SMPP-server traffic,
    `smpp_user` through the connector URL's query string.
    """
    token = request.GET.get("token", "") or request.POST.get("token", "")
    if not hmac.compare_digest(token, settings.INGEST_TOKEN):
        return HttpResponseForbidden("bad token")
    p = request.POST
    body = p.get("content", "")
    if not body and p.get("binary"):
        try:
            body = bytes.fromhex(p["binary"]).decode("utf-16-be" if p.get("coding") == "8" else "latin-1")
        except ValueError:
            body = p["binary"]
    routing.receive(source=p.get("from", ""), destination=p.get("to", ""), body=body,
                    origin_connector=p.get("origin-connector", ""), smpp_username=request.GET.get("smpp_user", ""),
                    source_ton=_int(p.get("source_addr_ton")), source_npi=_int(p.get("source_addr_npi")),
                    dest_ton=_int(p.get("dest_addr_ton")), dest_npi=_int(p.get("dest_addr_npi")))
    # Jasmin treats the exact body "ACK/Jasmin" as delivered.
    return HttpResponse("ACK/Jasmin")


def _client_ip(request):
    fwd = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return fwd.split(",")[0].strip() if fwd else request.META.get("REMOTE_ADDR", "")


@csrf_exempt
def api_inbound(request):
    """A supplier's HTTP bind: they send SMS for our numbers here instead of over SMPP.

    GET or POST /api/sms/inbound/ with key (or header X-Api-Key), to, from, message.
    Also accepted: msisdn/destination for to, sender/source for from, text/content for message.
    Answers JSON {"status": "ok", "id": "<uuid>"}.
    """
    from crm.models import HttpUser
    p = request.POST if request.method == "POST" else request.GET
    key = request.headers.get("X-Api-Key") or p.get("key", "")
    user = HttpUser.objects.select_related("account").filter(api_key=key, enabled=True).first() if key else None
    if not user:
        return JsonResponse({"status": "error", "error": "invalid key"}, status=403)
    if not user.allows(_client_ip(request)):
        return JsonResponse({"status": "error", "error": "IP not allowed"}, status=403)

    def first(*names):
        return next((p[n] for n in names if p.get(n)), "")

    to, frm, text = first("to", "msisdn", "destination"), first("from", "sender", "source"), first(
        "message", "text", "content")
    if not to or not text:
        return JsonResponse({"status": "error", "error": "to and message are required"}, status=400)
    msg = routing.receive(source=frm, destination=to, body=text, http_user=user)
    type(user).objects.filter(pk=user.pk).update(last_used=msg.received_at)
    return JsonResponse({"status": "ok", "id": str(msg.uuid), "routed": msg.client_id is not None})
