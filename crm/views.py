from datetime import timedelta

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.access import client_account, staff_required
from core.models import log
from sms.views import _date_range
from wholesale.models import ApiKey as WKey, BuyRate, SellRate, VendorBind as VBind
from . import models as m
from . import statements as st


class AccountForm(forms.ModelForm):
    class Meta:
        model = m.Account
        fields = ["title", "is_supplier", "is_client", "currency", "payment_terms", "account_manager",
                  "billing_email", "auto_statements", "tags", "process_insights", "active", "notes"]
        labels = {"is_supplier": "Supplier", "is_client": "Client", "account_manager": "Account manager",
                  "auto_statements": "Email monthly statement / invoice automatically"}
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}


# Every panel on the account page. kind -> (model, fields, singular label, group)
PANELS = {
    "smpp-user": (m.SmppUser, ["title", "role", "username", "password", "ip", "system_type", "dlr_wait_time",
                               "enabled"], "SMPP user (binds to us)", "sms"),
    "http-user": (m.HttpUser, ["title", "api_key", "ip", "enabled"], "HTTP user (supplier sends SMS to us over HTTP)",
                  "sms"),
    "smsc": (m.Smsc, ["title", "ip", "port", "username", "password", "system_type", "src_ton", "dst_ton",
                      "transceiver", "routing_attrs", "enforce_dlr", "enabled"], "SMSC (we bind to supplier)", "sms"),
    "http-endpoint": (m.HttpEndpoint, ["title", "url", "method", "to_param", "from_param", "message_param",
                                       "uuid_param", "enabled"], "HTTP endpoint (we push SMS to client)", "sms"),
    "wholesale-key": (WKey, ["title", "key", "ip", "dlr_url", "tps", "test_mode", "test_delivered_pct",
                             "test_price", "enabled"], "Wholesale HTTP key (customer sends traffic over HTTP)",
                      "wholesale"),
    "vendor-bind": (VBind, ["title", "host", "port", "username", "password", "system_type", "bind_mode", "tps",
                            "window", "src_ton", "src_npi", "enabled"], "Vendor bind (we bind to the vendor)",
                    "wholesale"),
    "sell-rate": (SellRate, ["country", "network", "rate"], "Sell rate (customer pays us)", "wholesale"),
    "buy-rate": (BuyRate, ["country", "network", "rate"], "Buy rate (we pay the vendor)", "wholesale"),
    "prefix": (m.Prefix, ["prefix"], "Supplier prefix", "sms"),
    "notification": (m.Notification, ["url"], "Notification URL", "sms"),
    "origination-ip": (m.OriginationIp, ["title", "ip", "ip_type"], "Origination IP", "voice"),
    "termination-ip": (m.TerminationIp, ["title", "ip", "priority"], "Termination IP", "voice"),
    "contact": (m.Contact, ["title", "first_name", "surname", "email", "telephone", "skype", "telegram", "whatsapp"],
                "Contact", "details"),
    "address": (m.Address, ["title", "address1", "address2", "address3", "postcode", "country"], "Address", "details"),
    "bank": (m.BankAccount, ["title", "bank_name", "account_name", "iban", "swift", "account_number", "sort_code",
                             "currency"], "Bank account", "details"),
}
SECRET_FIELDS = {"password"}


def panel_form(kind):
    model, fields, _, _ = PANELS[kind]
    widgets = {f: forms.PasswordInput(render_value=True) for f in fields if f in SECRET_FIELDS}
    return forms.modelform_factory(model, fields=fields, widgets=widgets)


@staff_required
def account_list(request, kind=None):
    if kind not in ("supplier", "client"):
        k = request.GET.get("kind")
        return redirect("/clients/" if k == "client" else "/suppliers/")
    if kind == "supplier":
        qs = m.Account.objects.filter(is_supplier=True).annotate(
            n_groups=Count("billing_groups", distinct=True),
            n_numbers=Count("billing_groups__numbers", distinct=True),
            n_binds=Count("smpp_users", distinct=True) + Count("smscs", distinct=True)
            + Count("http_users", distinct=True))
    else:
        qs = m.Account.objects.filter(is_client=True).annotate(
            n_numbers=Count("allocations__number", filter=Q(allocations__ends_at__isnull=True), distinct=True),
            n_http=Count("http_endpoints", filter=Q(http_endpoints__enabled=True), distinct=True),
            n_smpp=Count("smpp_users", filter=Q(smpp_users__role="client", smpp_users__enabled=True),
                         distinct=True))
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(tags__icontains=q))
    return render(request, "crm/account_list.html", {"accounts": qs, "kind": kind, "q": q,
                                                     "nav": kind + "s"})


@staff_required
def account_edit(request, pk=None):
    obj = get_object_or_404(m.Account, pk=pk) if pk else None
    kind = request.GET.get("kind")
    initial = {"is_supplier": kind == "supplier", "is_client": kind == "client"} if not obj else None
    form = AccountForm(request.POST or None, instance=obj, initial=initial)
    if request.method == "POST" and form.is_valid():
        acc = form.save()
        log(request.user, "account.save", acc)
        messages.success(request, f"Saved {acc}.")
        return redirect("account_detail", acc.pk)
    return render(request, "crm/account_form.html", {
        "form": form, "obj": obj, "nav": obj.nav_key if obj else (kind or "supplier") + "s",
        "list_url": obj.list_url if obj else f"/{kind or 'supplier'}s/",
        "list_label": obj.list_label if obj else ("Clients" if kind == "client" else "Suppliers")})


@staff_required
def account_detail(request, pk):
    acc = get_object_or_404(m.Account, pk=pk)
    tab = request.GET.get("tab", "connections")
    if tab == "wholesale":
        return redirect("ws_account", acc.pk)
    panels = []
    for kind, (model, fields, label, group) in PANELS.items():
        rows = model.objects.filter(account=acc)
        cols = [f for f in fields if f not in SECRET_FIELDS][:5]
        panels.append({"kind": kind, "label": label, "group": group, "rows": rows,
                       "cols": [model._meta.get_field(c).verbose_name for c in cols], "fields": cols})
    from inventory.models import Allocation, BillingGroup
    ctx = {"acc": acc, "tab": tab, "panels": panels, "nav": acc.nav_key,
           "groups": BillingGroup.objects.filter(supplier=acc).select_related("country", "currency"),
           "allocations": Allocation.objects.filter(client=acc, ends_at__isnull=True)
                          .select_related("number__billing_group__country")[:200],
           "users": acc.profile_set.select_related("user")}
    if tab == "statement":
        ctx.update(statement_context(request, acc))
        if request.GET.get("export") == "csv":
            resp = HttpResponse(st.rows_csv(ctx["rows"], ctx["by_label"]), content_type="text/csv")
            resp["Content-Disposition"] = (f'attachment; filename="{acc.title} {ctx["side"]} '
                                           f'{ctx["start"]:%Y%m%d}-{ctx["end"]:%Y%m%d}.csv"')
            return resp
    return render(request, "crm/account_detail.html", ctx)


@login_required
def client_statements(request):
    """Client portal: the client's own volumes and monthly statements (what we owe them)."""
    acc = client_account(request.user)
    if not acc:
        return redirect("client_list")
    ctx = statement_context(request, acc)
    ctx["breakdowns"] = [b for b in st.BREAKDOWNS if b[0] != "group"]  # billing groups are internal
    if ctx["by"] == "group":
        ctx["by"] = "day"
        ctx["rows"] = st.volumes(acc, ctx["side"], ctx["start"], ctx["end"], ctx["channel"], ctx["by"])
        ctx["t"], ctx["by_label"] = st.totals(ctx["rows"]), dict(st.BREAKDOWNS)[ctx["by"]]
    if request.GET.get("export") == "csv":
        resp = HttpResponse(st.rows_csv(ctx["rows"], ctx["by_label"]), content_type="text/csv")
        resp["Content-Disposition"] = f'attachment; filename="Statement {ctx["start"]:%Y%m%d}-{ctx["end"]:%Y%m%d}.csv"'
        return resp
    ctx.update({"acc": acc, "nav": "statements", "client_view": True, "sms_amount_label": "Earned"})
    return render(request, "crm/client_statements.html", ctx)


def statement_context(request, acc):
    f = request.GET
    sides = st.sides(acc) or [st.SUPPLIER]
    side = f.get("side") if f.get("side") in sides else sides[0]
    if f.get("date_range"):
        start, end = _date_range(f)
    else:  # default: this month so far
        first = timezone.localdate().replace(day=1)
        start, end = st.aware_range(first, timezone.localdate())
    channel = f.get("channel") if f.get("channel") in ("sms", "voice") else "all"
    by = f.get("by") if f.get("by") in dict(st.BREAKDOWNS) else "day"
    rows = st.volumes(acc, side, start, end, channel, by)
    docs = {x.period_start: x for x in acc.documents.filter(kind=st.kind_for(side))}
    months = []
    d = timezone.localdate().replace(day=1)
    for _ in range(6):
        d = (d - timedelta(days=1)).replace(day=1)
        months.append(d)
    return {"rows": rows, "t": st.totals(rows), "side": side, "sides": sides, "channel": channel, "by": by,
            "by_label": dict(st.BREAKDOWNS)[by], "breakdowns": st.BREAKDOWNS, "start": start, "end": end,
            "date_range": f"{start.astimezone():%Y-%m-%d %H:%M} - {end.astimezone():%Y-%m-%d %H:%M}",
            "doc_kind": st.kind_for(side),
            "month_rows": [(d, docs.get(d)) for d in months],
            "recipients": st.recipients(acc), "sms_amount_label": "Due in" if side == st.SUPPLIER else "Due out"}


@login_required
def document_view(request, pk, kind, month):
    acc = get_object_or_404(m.Account, pk=pk)
    if kind not in dict(m.Document.KINDS):
        raise Http404
    own = client_account(request.user)
    if own and (own.pk != acc.pk or kind != m.Document.STATEMENT):
        raise PermissionDenied  # a client sees only its own statements
    try:
        first, last = st.month_bounds(month)
    except ValueError:
        raise Http404
    doc = m.Document.objects.filter(account=acc, kind=kind, period_start=first).first()
    if request.GET.get("export") == "csv":
        ctx = st.document_context(acc, kind, first, last, doc)
        resp = HttpResponse(st.rows_csv(ctx["daily"], "Day"), content_type="text/csv")
        resp["Content-Disposition"] = f'attachment; filename="{ctx["number"]}.csv"'
        return resp
    return render(request, "crm/document.html", st.document_context(acc, kind, first, last, doc))


@staff_required
@require_POST
def document_send(request, pk):
    acc = get_object_or_404(m.Account, pk=pk)
    kind, month = request.POST.get("kind"), request.POST.get("month")
    if kind not in dict(m.Document.KINDS):
        raise Http404
    try:
        first, _ = st.month_bounds(month)
    except (TypeError, ValueError):
        raise Http404
    doc = st.create_document(acc, kind, first)
    if doc.sent_at and not request.POST.get("resend"):
        messages.info(request, f"{doc} was already sent on {doc.sent_at:%d %b %Y}.")
    elif st.send_document(doc):
        log(request.user, "document.send", acc, number=doc.number, to=doc.sent_to)
        messages.success(request, f"Sent {doc} to {doc.sent_to}.")
    else:
        messages.error(request, f"Couldn't send {doc}: {doc.last_error}")
    return redirect(f"/accounts/{acc.pk}/?tab=statement&side={st.side_for(kind)}")


@staff_required
def panel_edit(request, pk, kind, item=None):
    if kind not in PANELS:
        raise Http404
    acc = get_object_or_404(m.Account, pk=pk)
    model, _, label, group = PANELS[kind]
    obj = get_object_or_404(model, pk=item, account=acc) if item else None
    Form = panel_form(kind)
    form = Form(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        o = form.save(commit=False)
        o.account = acc
        o.save()
        log(request.user, f"{kind}.save", f"{acc} / {o}")
        messages.success(request, f"Saved {label.split(' (')[0].lower()} “{o}”.")
        return redirect(f"/accounts/{acc.pk}/?tab={group}")
    return render(request, "crm/panel_form.html", {"form": form, "acc": acc, "label": label, "obj": obj,
                                                   "kind": kind, "group": group, "nav": acc.nav_key})


@staff_required
@require_POST
def panel_delete(request, pk, kind, item):
    if kind not in PANELS:
        raise Http404
    acc = get_object_or_404(m.Account, pk=pk)
    model, _, label, group = PANELS[kind]
    obj = get_object_or_404(model, pk=item, account=acc)
    log(request.user, f"{kind}.delete", f"{acc} / {obj}")
    obj.delete()
    messages.success(request, f"Deleted {label.split(' (')[0].lower()}.")
    return redirect(f"/accounts/{acc.pk}/?tab={group}")


@staff_required
def bind_status(request):
    return render(request, "crm/bind_status.html", {
        "smpp_users": m.SmppUser.objects.select_related("account").order_by("role", "account__title"),
        "http_users": m.HttpUser.objects.select_related("account"),
        "http_endpoints": m.HttpEndpoint.objects.select_related("account"),
        "smscs": m.Smsc.objects.select_related("account"), "nav": "binds"})
