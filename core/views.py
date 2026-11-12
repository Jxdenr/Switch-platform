from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.access import client_account
from crm.models import SmppUser, Smsc
from inventory.models import Allocation, Number
from sms.models import Message
from voice.models import Call


@login_required
def dashboard(request):
    from . import dashboard as d
    acc = client_account(request.user)
    period = request.GET.get("period") if request.GET.get("period") in dict(d.PERIODS) else "7d"
    channel = request.GET.get("channel") if request.GET.get("channel") in dict(d.CHANNELS) else "all"
    start, end, (pstart, pend) = d.period_bounds(period)
    now_t, prev_t = d.totals(start, end, channel, acc), d.totals(pstart, pend, channel, acc)
    ctx = {"nav": "dashboard", "is_client": bool(acc), "period": period, "channel": channel,
           "periods": d.PERIODS, "channels": d.CHANNELS, "period_label": dict(d.PERIODS)[period],
           "t": now_t, "delta": {k: d.change(now_t[k], prev_t[k]) for k in ("due_in", "due_out", "margin", "sms",
                                                                              "minutes")},
           "chart": d.chart(d.series(start, end, channel, period == "today", acc)),
           "has_voice": Call.objects.exists()}
    if not acc:
        failed = Message.objects.filter(status=Message.FAILED)
        n_numbers = Number.objects.count()
        n_alloc = Allocation.objects.filter(ends_at__isnull=True).values("number").distinct().count()
        ctx.update({
            "top_suppliers": d.top_accounts(start, end, channel, "supplier"),
            "top_clients": d.top_accounts(start, end, channel, "client"),
            "down": (list(SmppUser.objects.filter(enabled=True, bound=False).select_related("account"))
                     + list(Smsc.objects.filter(enabled=True, bound=False).select_related("account"))),
            "n_failed": failed.count(), "recent_failed": failed.select_related("client")[:5],
            "n_numbers": n_numbers, "n_alloc": n_alloc,
            "alloc_pct": (n_alloc / n_numbers * 100) if n_numbers else 0})
    return render(request, "core/dashboard.html", ctx)


@login_required
@require_POST
def set_ui(request):
    p = request.user.profile
    if request.POST.get("colourway") in ("harbour", "graphite", "paper"):
        p.colourway = request.POST["colourway"]
    if request.POST.get("layout") in ("sidebar", "top", "rail"):
        p.layout = request.POST["layout"]
    p.save()
    resp = redirect(request.POST.get("back") or "dashboard")
    resp.delete_cookie("cw")
    resp.delete_cookie("layout")
    return resp


def health(request):
    return HttpResponse("ok")
