from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.access import machine_endpoint, client_account, portal_view, require
from crm.models import SmppUser, Smsc
from inventory.models import Allocation, Number
from sms.models import Message
from voice.models import Call


@portal_view()
def dashboard(request):
    from . import dashboard as d
    acc = client_account(request.user)
    period = request.GET.get("period") if request.GET.get("period") in dict(d.PERIODS) else "7d"
    channel = request.GET.get("channel") if request.GET.get("channel") in dict(d.CHANNELS) else "all"
    start, end, (pstart, pend) = d.period_bounds(period)
    # Money in different currencies is never added up: headline the biggest one, list the others.
    cur_rows = d.currencies(start, end, channel, acc)
    cur = cur_rows[0][0] if cur_rows else d.main_currency(pstart, pend, channel, acc)
    now_t, prev_t = d.totals(start, end, channel, acc, cur), d.totals(pstart, pend, channel, acc, cur)
    sms_all, sms_prev = d.totals(start, end, channel, acc), d.totals(pstart, pend, channel, acc)
    now_t.update(sms=sms_all["sms"], minutes=sms_all["minutes"], calls=sms_all["calls"])
    from .models import Currency
    sym = {c.code: c.symbol for c in Currency.objects.all()}
    ctx = {"nav": "dashboard", "is_client": bool(acc), "period": period, "channel": channel,
           "periods": d.PERIODS, "channels": d.CHANNELS, "period_label": dict(d.PERIODS)[period],
           "cur": cur, "sym": sym.get(cur, ""),
           "other_currencies": [(c, sym.get(c, ""), i, o) for c, i, o in cur_rows[1:]],
           "t": now_t, "delta": {"due_in": d.change(now_t["due_in"], prev_t["due_in"]),
                                 "due_out": d.change(now_t["due_out"], prev_t["due_out"]),
                                 "sms": d.change(sms_all["sms"], sms_prev["sms"]),
                                 "minutes": d.change(sms_all["minutes"], sms_prev["minutes"])},
           "dl": d.delivery(start, end, channel, acc),
           "chart": d.chart(d.series(start, end, channel, period == "today", acc, cur)),
           "has_voice": Call.objects.exists()}
    if acc:
        from crm.models import HttpEndpoint
        live = Allocation.objects.filter(client=acc, ends_at__isnull=True)
        ctx.update({
            "my_numbers": live.values("number").distinct().count(),
            "my_sms_numbers": live.filter(channel="sms").count(), "my_voice_numbers": live.filter(channel="voice").count(),
            "my_failed": Message.objects.filter(client=acc, status=Message.FAILED, received_at__gte=start,
                                                received_at__lt=end).count(),
            "my_endpoints": HttpEndpoint.objects.filter(account=acc),
            "my_binds": SmppUser.objects.filter(account=acc, role="client")})
    else:
        failed = Message.objects.filter(status=Message.FAILED)
        n_numbers = Number.objects.count()
        n_alloc = Allocation.objects.filter(ends_at__isnull=True).values("number").distinct().count()
        ctx.update({
            "unrouted": d.unrouted(start, end, channel),
            "top_suppliers": d.top_accounts(start, end, channel, "supplier", cur),
            "top_clients": d.top_accounts(start, end, channel, "client", cur),
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


@machine_endpoint
def health(request):
    return HttpResponse("ok")
