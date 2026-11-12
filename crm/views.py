from django import forms
from django.contrib import messages
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.access import staff_required
from core.models import log
from . import models as m


class AccountForm(forms.ModelForm):
    class Meta:
        model = m.Account
        fields = ["title", "is_supplier", "is_client", "currency", "payment_terms", "account_manager",
                  "tags", "process_insights", "active", "notes"]
        labels = {"is_supplier": "Supplier", "is_client": "Client", "account_manager": "Account manager"}
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}


# Every panel on the account page. kind -> (model, fields, singular label, group)
PANELS = {
    "smpp-user": (m.SmppUser, ["title", "username", "password", "ip", "system_type", "dlr_wait_time", "enabled"],
                  "SMPP user (supplier binds to us)", "sms"),
    "smsc": (m.Smsc, ["title", "ip", "port", "username", "password", "system_type", "src_ton", "dst_ton",
                      "transceiver", "routing_attrs", "enforce_dlr", "enabled"], "SMSC (we bind to supplier)", "sms"),
    "http-endpoint": (m.HttpEndpoint, ["title", "url", "method", "to_param", "from_param", "message_param",
                                       "uuid_param", "enabled"], "HTTP endpoint (we push SMS to client)", "sms"),
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
def account_list(request):
    qs = m.Account.objects.annotate(
        n_numbers=Count("allocations", filter=Q(allocations__ends_at__isnull=True), distinct=True),
        n_groups=Count("billing_groups", distinct=True),
    )
    kind = request.GET.get("kind", "")
    if kind == "supplier":
        qs = qs.filter(is_supplier=True)
    elif kind == "client":
        qs = qs.filter(is_client=True)
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(tags__icontains=q))
    return render(request, "crm/account_list.html", {"accounts": qs, "kind": kind, "q": q,
                                                     "nav": "accounts"})


@staff_required
def account_edit(request, pk=None):
    obj = get_object_or_404(m.Account, pk=pk) if pk else None
    form = AccountForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        acc = form.save()
        log(request.user, "account.save", acc)
        messages.success(request, f"Saved {acc}.")
        return redirect("account_detail", acc.pk)
    return render(request, "crm/account_form.html", {"form": form, "obj": obj, "nav": "accounts"})


@staff_required
def account_detail(request, pk):
    acc = get_object_or_404(m.Account, pk=pk)
    tab = request.GET.get("tab", "connections")
    panels = []
    for kind, (model, fields, label, group) in PANELS.items():
        rows = model.objects.filter(account=acc)
        cols = [f for f in fields if f not in SECRET_FIELDS][:5]
        panels.append({"kind": kind, "label": label, "group": group, "rows": rows,
                       "cols": [model._meta.get_field(c).verbose_name for c in cols], "fields": cols})
    from inventory.models import Allocation, BillingGroup
    ctx = {"acc": acc, "tab": tab, "panels": panels, "nav": "accounts",
           "groups": BillingGroup.objects.filter(supplier=acc).select_related("country", "currency"),
           "allocations": Allocation.objects.filter(client=acc, ends_at__isnull=True)
                          .select_related("number__billing_group__country")[:200],
           "users": acc.profile_set.select_related("user")}
    return render(request, "crm/account_detail.html", ctx)


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
                                                   "kind": kind, "group": group, "nav": "accounts"})


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
        "smpp_users": m.SmppUser.objects.select_related("account"),
        "smscs": m.Smsc.objects.select_related("account"), "nav": "binds"})
