from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q, Sum
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
    now = timezone.now()
    acc = client_account(request.user)
    base = Message.objects.exclude(status__in=[Message.NO_ROUTE, Message.BLOCKED])
    calls = Call.objects.exclude(client__isnull=True)
    if acc:
        base, calls = base.filter(client=acc), calls.filter(client=acc)
    periods = []
    for label, since in [("Today", timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)),
                         ("7 days", now - timedelta(days=7)), ("Month", now - timedelta(days=30))]:
        a = base.filter(received_at__gte=since).aggregate(n=Count("pk"), due_in=Sum("rate_in"), due_out=Sum("rate_out"))
        a["margin"] = (a["due_in"] or 0) - (a["due_out"] or 0)
        v = calls.filter(start_stamp__gte=since).aggregate(n=Count("pk"), connected=Count("pk", filter=Q(billsec__gt=0)),
                                                           secs=Sum("billable_seconds"), due_in=Sum("due_in"),
                                                           due_out=Sum("due_out"))
        v["minutes"] = (v["secs"] or 0) / 60
        v["margin"] = (v["due_in"] or 0) - (v["due_out"] or 0)
        periods.append((label, a, v))
    down = [] if acc else (list(SmppUser.objects.filter(enabled=True, bound=False).select_related("account")) +
                           list(Smsc.objects.filter(enabled=True, bound=False).select_related("account")))
    recent_failed = base.filter(status=Message.FAILED).select_related("client")[:8]
    return render(request, "core/dashboard.html", {
        "periods": periods, "down": down, "recent_failed": recent_failed, "nav": "dashboard",
        "n_numbers": Number.objects.count(), "n_alloc": Allocation.objects.filter(ends_at__isnull=True).count(),
        "is_client": bool(acc), "has_voice": Call.objects.exists()})


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
