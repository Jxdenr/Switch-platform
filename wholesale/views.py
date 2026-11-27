import csv
import io
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django import forms
from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, F, Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from core.access import portal_view, require
from core.models import Country, log
from crm.models import Account, SmppUser
from sms.views import _date_range
from .models import (ApiKey, BuyRate, Customer, LedgerEntry, Network, NetworkPrefix, OutboundMessage as M, RouteEntry,
                     RoutePlan, SellRate, SmppTestSettings, Vendor, VendorBind, wholesale_currency)
from .quality import vendor_quality


def _today():
    now = timezone.localtime()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def totals(qs):
    t = qs.aggregate(n=Count("pk"), refused=Count("pk", filter=Q(status=M.REFUSED)),
                     delivered=Count("pk", filter=Q(status=M.DELIVERED)),
                     final=Count("pk", filter=Q(status__in=M.FINAL)),
                     revenue=Sum("charged"), cost=Sum("buy_rate", filter=Q(vendor__isnull=False)))
    t["revenue"], t["cost"] = t["revenue"] or Decimal(0), t["cost"] or Decimal(0)
    t["margin"] = t["revenue"] - t["cost"]
    t["delivered_pct"] = round(100.0 * t["delivered"] / t["final"], 1) if t["final"] else None
    return t


@require("wholesale")
def overview(request):
    since = timezone.now() - timedelta(hours=24)
    live = M.objects.filter(created_at__gte=since, test=False)
    customers = (Customer.objects.select_related("account", "route_plan")
                 .annotate(n_today=Count("account__wholesale_sent", filter=Q(account__wholesale_sent__created_at__gte=since))))
    vendors = list(Vendor.objects.select_related("account").annotate(
        n_binds=Count("account__vendor_binds", filter=Q(account__vendor_binds__enabled=True), distinct=True),
        n_up=Count("account__vendor_binds", filter=Q(account__vendor_binds__enabled=True,
                                                    account__vendor_binds__bound=True), distinct=True)))
    q = {r["vendor"].account_id: r for r in vendor_quality(days=1)}
    for v in vendors:
        v.q = q.get(v.account_id)
    return render(request, "wholesale/overview.html", {
        "nav": "ws_overview", "t": totals(live), "customers": customers, "vendors": vendors,
        "currency": wholesale_currency(), "queued": M.objects.filter(status=M.QUEUED).count(),
        "waiting": M.objects.filter(status=M.SUBMITTED).count(),
        "dlr_backlog": M.objects.filter(dlr_state__in=[M.DLR_PENDING, M.DLR_FAILED]).count()})


MSG_FILTERS = [("customer", "customer_id"), ("vendor", "vendor_id"), ("status", "status"), ("stat", "stat")]


@require("wholesale")
def message_list(request):
    f = request.GET
    start, end = _date_range(f)
    qs = M.objects.filter(created_at__range=(start, end)).select_related("customer", "vendor", "network", "country")
    for key, field in MSG_FILTERS:
        if f.get(key):
            qs = qs.filter(**{field: f[key]})
    if f.get("destination"):
        qs = qs.filter(destination__contains=f["destination"].lstrip("+"))
    if f.get("id"):
        qs = qs.filter(Q(message_id=f["id"].strip().lower()) | Q(vendor_message_id=f["id"].strip().lower()))
    if f.get("test") in ("0", "1"):
        qs = qs.filter(test=f["test"] == "1")
    if f.get("export") == "csv":
        resp = HttpResponse(content_type="text/csv")
        resp["Content-Disposition"] = f'attachment; filename="wholesale-{start:%Y%m%d}-{end:%Y%m%d}.csv"'
        w = csv.writer(resp)
        w.writerow(["Date", "Our ID", "Customer", "Vendor", "Vendor ID", "From", "To", "Network", "Status", "Stat",
                    "Err", "Reason", "Sell", "Buy", "Charged", "Test", "Receipt sent"])
        for m in qs.iterator():
            w.writerow([m.created_at.isoformat(), m.message_id, m.customer, m.vendor or "", m.vendor_message_id,
                        m.source, m.destination, m.network or m.country or "", m.get_status_display(), m.stat,
                        m.err, m.reason, m.sell_rate, m.buy_rate if m.vendor_id else "", m.charged,
                        "yes" if m.test else "", m.get_dlr_state_display()])
        return resp
    page = Paginator(qs, 100).get_page(f.get("page"))
    return render(request, "wholesale/messages.html", {
        "nav": "ws_messages", "page": page, "t": totals(qs), "currency": wholesale_currency(),
        "date_range": f"{start.astimezone():%Y-%m-%d %H:%M} - {end.astimezone():%Y-%m-%d %H:%M}",
        "customers": Account.objects.filter(wholesale_customer__isnull=False),
        "vendors": Account.objects.filter(wholesale_vendor__isnull=False), "statuses": M.STATUSES, "f": f})


@require("wholesale")
def quality(request):
    days = int(request.GET.get("days") or 7) if (request.GET.get("days") or "7").isdigit() else 7
    return render(request, "wholesale/quality.html", {"nav": "ws_quality", "rows": vendor_quality(days), "days": days})


# ---------- route plans ----------

class PlanForm(forms.ModelForm):
    class Meta:
        model = RoutePlan
        fields = ["title", "mode", "min_delivery_pct", "notes"]


class EntryForm(forms.ModelForm):
    class Meta:
        model = RouteEntry
        fields = ["country", "network", "vendor", "priority"]

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fields["vendor"].queryset = Account.objects.filter(wholesale_vendor__isnull=False)
        self.fields["country"].empty_label = "All countries"
        self.fields["network"].empty_label = "All networks"


@require("wholesale")
def routes(request):
    plan_form = PlanForm(request.POST if request.POST.get("form") == "plan" else None)
    if request.method == "POST":
        if request.POST.get("delete_entry"):
            RouteEntry.objects.filter(pk=request.POST["delete_entry"]).delete()
            return redirect(request.get_full_path())
        if request.POST.get("form") == "plan" and plan_form.is_valid():
            p = plan_form.save()
            log(request.user, "wholesale.plan.add", p)
            messages.success(request, f"Added route plan {p}.")
            return redirect(request.get_full_path())
        if request.POST.get("form") == "entry":
            plan = get_object_or_404(RoutePlan, pk=request.POST.get("plan"))
            ef = EntryForm(request.POST)
            if ef.is_valid():
                e = ef.save(commit=False)
                e.plan = plan
                e.save()
                messages.success(request, f"Added {e.vendor} to {plan}.")
            else:
                messages.error(request, "Choose a vendor.")
            return redirect(request.get_full_path())
    plans = RoutePlan.objects.prefetch_related("entries__country", "entries__network", "entries__vendor").annotate(
        n_customers=Count("customer"))
    return render(request, "wholesale/routes.html", {"nav": "ws_routes", "plans": plans, "plan_form": plan_form,
                                                     "entry_form": EntryForm()})


# ---------- networks ----------

def find_country(v):
    v = (v or "").strip()
    return (Country.objects.filter(Q(name__iexact=v) | Q(iso2__iexact=v)).first()
            or Country.objects.filter(dial_code=v.lstrip("+")).first() if v else None)


@require("wholesale")
def networks(request):
    if request.method == "POST" and request.FILES.get("csv"):
        added, errors = import_networks(request.FILES["csv"].read().decode("utf-8-sig", "replace"))
        log(request.user, "wholesale.networks.import", added=added)
        messages.success(request, f"Imported {added} prefixes.")
        for e in errors[:10]:
            messages.error(request, e)
        return redirect("ws_networks")
    nets = Network.objects.select_related("country").annotate(n_prefixes=Count("prefixes"))
    return render(request, "wholesale/networks.html", {"nav": "ws_networks", "networks": nets})


def import_networks(text):
    """CSV: country, network, mcc, mnc, prefix (one prefix per row, or several separated by spaces)."""
    added, errors = 0, []
    for i, row in enumerate(csv.reader(io.StringIO(text)), 1):
        if len(row) < 5 or not row[2].strip().isdigit():
            if i > 1 or (row and row[0].lower() != "country"):
                errors.append(f"Row {i}: expected country, network, mcc, mnc, prefix")
            continue
        country = find_country(row[0])
        if not country:
            errors.append(f"Row {i}: unknown country {row[0]!r}")
            continue
        net, _ = Network.objects.get_or_create(mcc=row[2].strip(), mnc=row[3].strip().zfill(2),
                                               defaults={"country": country, "name": row[1].strip()})
        for p in row[4].split():
            p = p.strip().lstrip("+")
            if p.isdigit():
                NetworkPrefix.objects.update_or_create(prefix=p, defaults={"network": net})
                added += 1
    return added, errors


# ---------- per account ----------

class CustomerForm(forms.ModelForm):
    class Meta:
        model = Customer
        fields = ["enabled", "route_plan", "charge_on", "credit_limit", "allow_loss", "allowed_senders",
                  "low_balance_alert"]
        widgets = {"charge_on": forms.RadioSelect}


class VendorForm(forms.ModelForm):
    class Meta:
        model = Vendor
        fields = ["enabled", "route_type", "notes"]


class TopUpForm(forms.Form):
    amount = forms.DecimalField(max_digits=14, decimal_places=5, help_text="Negative to take credit off")
    note = forms.CharField(max_length=200, required=False)


@require("wholesale")
def account(request, pk):
    acc = get_object_or_404(Account, pk=pk)
    cust = Customer.objects.filter(account=acc).first()
    vend = Vendor.objects.filter(account=acc).first()
    action = request.POST.get("action")
    if request.method == "POST":
        if action == "enable_customer" and not cust:
            Customer.objects.create(account=acc)
            messages.success(request, f"{acc} can now send wholesale traffic (prepaid, balance 0).")
        elif action == "enable_vendor" and not vend:
            Vendor.objects.create(account=acc)
            messages.success(request, f"{acc} is now a vendor. Add a bind and buy rates.")
        elif action == "customer" and cust:
            form = CustomerForm(request.POST, instance=cust)
            if form.is_valid():
                form.save()
                log(request.user, "wholesale.customer.save", acc)
                messages.success(request, "Saved customer settings.")
        elif action == "vendor" and vend:
            form = VendorForm(request.POST, instance=vend)
            if form.is_valid():
                form.save()
                messages.success(request, "Saved vendor settings.")
        elif action == "topup" and cust:
            form = TopUpForm(request.POST)
            if form.is_valid():
                with transaction.atomic():
                    LedgerEntry.objects.create(customer=cust, amount=form.cleaned_data["amount"],
                                               note=form.cleaned_data["note"], user=request.user)
                    Customer.objects.filter(pk=cust.pk).update(balance=F("balance") + form.cleaned_data["amount"])
                log(request.user, "wholesale.topup", acc, amount=str(form.cleaned_data["amount"]))
                messages.success(request, f"Balance changed by {form.cleaned_data['amount']}.")
        elif action in ("sell", "buy") and request.FILES.get("csv"):
            n, errors = import_rates(SellRate if action == "sell" else BuyRate, acc,
                                     request.FILES["csv"].read().decode("utf-8-sig", "replace"))
            log(request.user, f"wholesale.rates.{action}", acc, rows=n)
            messages.success(request, f"Imported {n} rates.")
            for e in errors[:10]:
                messages.error(request, e)
        return redirect("ws_account", acc.pk)
    since = _today() - timedelta(days=29)
    ctx = {"acc": acc, "cust": cust, "vend": vend, "nav": acc.nav_key, "currency": wholesale_currency(),
           "customer_form": CustomerForm(instance=cust) if cust else None,
           "vendor_form": VendorForm(instance=vend) if vend else None, "topup_form": TopUpForm(),
           "smpp_users": SmppUser.objects.filter(account=acc, role=SmppUser.WHOLESALE).select_related("wholesale"),
           "keys": ApiKey.objects.filter(account=acc), "binds": VendorBind.objects.filter(account=acc),
           "sell_rates": SellRate.objects.filter(account=acc).select_related("country", "network"),
           "buy_rates": BuyRate.objects.filter(account=acc).select_related("country", "network"),
           "ledger": cust.ledger.select_related("user")[:20] if cust else [],
           "sent30": totals(M.objects.filter(customer=acc, created_at__gte=since, test=False)) if cust else None,
           "carried30": totals(M.objects.filter(vendor=acc, created_at__gte=since, test=False)) if vend else None}
    return render(request, "wholesale/account.html", ctx)


def import_rates(model, acc, text):
    """CSV: country (name, ISO code or dial code), mcc-mnc (blank = whole country), rate."""
    n, errors = 0, []
    for i, row in enumerate(csv.reader(io.StringIO(text)), 1):
        if len(row) < 3:
            continue
        try:
            rate = Decimal(row[2].strip())
        except InvalidOperation:
            if i > 1:
                errors.append(f"Row {i}: rate {row[2]!r} is not a number")
            continue
        country = find_country(row[0])
        if not country:
            errors.append(f"Row {i}: unknown country {row[0]!r}")
            continue
        net = None
        if row[1].strip():
            mcc, _, mnc = row[1].strip().partition("-")
            net = Network.objects.filter(mcc=mcc, mnc=mnc.zfill(2)).first()
            if not net:
                errors.append(f"Row {i}: unknown network {row[1]!r}. Import it on the Networks page first.")
                continue
        model.objects.update_or_create(account=acc, country=country, network=net, defaults={"rate": rate})
        n += 1
    return n, errors


class SmppSettingsForm(forms.ModelForm):
    class Meta:
        model = SmppTestSettings
        fields = ["tps", "test_mode", "test_delivered_pct", "test_price"]


@require("wholesale")
def smpp_settings(request, pk):
    su = get_object_or_404(SmppUser, pk=pk, role=SmppUser.WHOLESALE)
    obj, _ = SmppTestSettings.objects.get_or_create(smpp_user=su)
    form = SmppSettingsForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        form.save()
        log(request.user, "wholesale.smpp_settings", su, test=obj.test_mode)
        messages.success(request, f"Saved {su}. Test mode applies straight away; a new speed limit when they next bind.")
        return redirect("ws_account", su.account_id)
    return render(request, "wholesale/smpp_settings.html", {"form": form, "su": su, "acc": su.account,
                                                            "nav": su.account.nav_key})
