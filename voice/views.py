import csv
import hmac
import json

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Avg, Count, F, Q, Sum
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from core.access import client_account, staff_required
from crm.models import Account
from sms.views import _date_range
from .billing import from_freeswitch_json, record_call
from .models import Call

CDR_COLUMNS = ["start_stamp", "end_stamp", "duration", "billsec", "billable_seconds", "pdd_ms", "number_called",
               "caller_id", "cnam", "billing_group", "supplier", "client", "service", "hangup_cause",
               "hangup_cause_q850", "proto_specific_cause", "sip_hangup_disposition", "mos_in", "from_ip",
               "remote_media_ip", "rate_in", "rate_out", "due_in", "due_out"]


def _filtered(request):
    f = request.GET
    start, end = _date_range(f)
    qs = Call.objects.filter(start_stamp__range=(start, end)).select_related("supplier", "client", "billing_group")
    acc = client_account(request.user)
    if acc:
        qs = qs.filter(client=acc)
    if f.get("supplier"):
        qs = qs.filter(supplier_id=f["supplier"])
    if f.get("client"):
        qs = qs.filter(client_id=f["client"])
    if f.get("number"):
        qs = qs.filter(number_called__contains=f["number"].lstrip("+"))
    if f.get("caller"):
        qs = qs.filter(caller_id__contains=f["caller"])
    if f.get("connected") == "1":
        qs = qs.filter(billsec__gt=0)
    elif f.get("connected") == "0":
        qs = qs.filter(billsec=0)
    return qs, start, end


@login_required
def cdrs(request):
    qs, start, end = _filtered(request)
    is_client = bool(client_account(request.user))
    if request.GET.get("export") == "csv":
        cols = [c for c in CDR_COLUMNS if not (is_client and c in ("rate_in", "due_in", "supplier", "from_ip"))]
        resp = HttpResponse(content_type="text/csv")
        resp["Content-Disposition"] = f'attachment; filename="cdrs-{start:%Y%m%d}-{end:%Y%m%d}.csv"'
        w = csv.writer(resp)
        w.writerow(cols)
        for c in qs.iterator(chunk_size=5000):
            w.writerow([getattr(c, k) if getattr(c, k) is not None else "" for k in cols])
        return resp
    totals = qs.aggregate(calls=Count("pk"), connected=Count("pk", filter=Q(billsec__gt=0)),
                          secs=Sum("billable_seconds"), due_in=Sum("due_in"), due_out=Sum("due_out"))
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    return render(request, "voice/cdrs.html", {
        "page": page, "f": request.GET, "totals": totals, "nav": "cdrs", "is_client": is_client,
        "minutes": (totals["secs"] or 0) / 60,
        "date_range": f"{start.astimezone():%Y-%m-%d %H:%M} - {end.astimezone():%Y-%m-%d %H:%M}",
        "suppliers": [] if is_client else Account.objects.filter(is_supplier=True),
        "clients": [] if is_client else Account.objects.filter(is_client=True)})


@staff_required
def stats(request):
    qs, start, end = _filtered(request)
    by = request.GET.get("by", "supplier_client")
    fields = {"supplier_client": ["supplier__title", "client__title", "payment_terms", "currency"],
              "supplier": ["supplier__title", "currency"], "client": ["client__title", "currency"],
              "group": ["billing_group__title", "supplier__title", "currency"],
              "day": ["start_stamp__date", "currency"]}[by]
    rows = list(qs.values(*fields).annotate(
        calls=Count("pk"), connected=Count("pk", filter=Q(billsec__gt=0)), secs=Sum("billable_seconds"),
        acd=Avg("billsec", filter=Q(billsec__gt=0)), due_in=Sum("due_in"), due_out=Sum("due_out"),
    ).annotate(margin=F("due_in") - F("due_out")).order_by(*fields))
    for r in rows:
        r["minutes"] = (r["secs"] or 0) / 60
        r["asr"] = (r["connected"] / r["calls"] * 100) if r["calls"] else 0
        r["pct"] = (r["margin"] / r["due_in"] * 100) if r["due_in"] else 0
    return render(request, "voice/stats.html", {
        "rows": rows, "by": by, "nav": "voicestats",
        "date_range": f"{start.astimezone():%Y-%m-%d %H:%M} - {end.astimezone():%Y-%m-%d %H:%M}"})


@csrf_exempt
@require_POST
def ingest_freeswitch(request):
    """FreeSWITCH mod_json_cdr posts one JSON CDR per call here (url ...?token=INGEST_TOKEN)."""
    if not hmac.compare_digest(request.GET.get("token", ""), settings.INGEST_TOKEN):
        return HttpResponseForbidden("bad token")
    raw = request.body
    if raw.startswith(b"cdr="):  # mod_json_cdr can send form-encoded
        from urllib.parse import unquote_plus
        raw = unquote_plus(raw[4:].decode()).encode()
    try:
        data = from_freeswitch_json(json.loads(raw))
    except (ValueError, KeyError) as e:
        return HttpResponseBadRequest(f"bad cdr: {e}")
    if not data["uuid"] or not data["start"]:
        return HttpResponseBadRequest("missing uuid or start time")
    if not Call.objects.filter(uuid=data["uuid"]).exists():  # FreeSWITCH may retry
        record_call(**data)
    return HttpResponse("ok")
