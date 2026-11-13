import csv

from django import forms
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Exists, OuterRef, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.access import client_account, staff_required
from core.models import Country, PaymentTerms, TimeOfDay
from crm.models import Account
from . import services
from .models import Allocation, BillingGroup, BillingGroupRate, Number


class BillingGroupForm(forms.ModelForm):
    class Meta:
        model = BillingGroup
        fields = ["supplier", "title", "routing_type", "number_type", "country", "network", "currency",
                  "blocked", "active", "note", "billing_increment_first", "billing_increment_next",
                  "min_duration", "allow_partial_match", "acd", "asr", "max_duration",
                  "restrict_overlapping", "concurrent_limit"]
        widgets = {"note": forms.Textarea(attrs={"rows": 2}), "routing_type": forms.RadioSelect}
        labels = {"billing_increment_first": "First billing block (sec)",
                  "billing_increment_next": "Then bill every (sec)", "min_duration": "Minimum billed (sec)",
                  "max_duration": "Maximum billed (sec)", "acd": "ACD target (sec)", "asr": "ASR target (%)",
                  "concurrent_limit": "Max concurrent calls", "allow_partial_match": "Allow partial number match",
                  "restrict_overlapping": "Restrict overlapping calls"}
        help_texts = {"title": "Shown to clients on their monthly statements.", "billing_increment_first": "60 and 60 = per-minute billing; 1 and 1 = per second.", "acd": "", "asr": ""}


class RateForm(forms.ModelForm):
    class Meta:
        model = BillingGroupRate
        fields = ["channel", "slot", "payment_terms", "time_of_day", "rate_in", "rate_out"]
        labels = {"rate_in": "Supplier pays us", "rate_out": "We pay the client"}

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.fields["payment_terms"].empty_label = "Choose terms"
        self.fields["time_of_day"].empty_label = "All day"
        for f in ("rate_in", "rate_out"):
            self.fields[f].widget.attrs.update({"placeholder": "0.00000", "step": "0.00001", "inputmode": "decimal"})


class BaseRateFormSet(forms.BaseInlineFormSet):
    routing_type = None

    def clean(self):
        super().clean()
        seen, channels = set(), None
        if self.routing_type:
            channels = BillingGroup(routing_type=self.routing_type).channels
        for f in self.forms:
            if not getattr(f, "cleaned_data", None) or f.cleaned_data.get("DELETE"):
                continue
            d = f.cleaned_data
            if not d.get("channel"):
                continue
            if channels and d["channel"] not in channels:
                raise forms.ValidationError(
                    f"There's a {d['channel'].upper()} rate, but this group is only used for "
                    f"{' and '.join(c.upper() for c in channels)}. Change “Used for” or clear that rate.")
            key = (d["channel"], d.get("slot"), d.get("time_of_day"))
            if key in seen:
                raise forms.ValidationError(
                    f"Two {d['channel'].upper()} rates use Terms {d.get('slot')}"
                    f"{' for ' + str(d['time_of_day']) if d.get('time_of_day') else ''}. Each needs its own slot.")
            seen.add(key)


RateFormSet = forms.inlineformset_factory(
    BillingGroup, BillingGroupRate, form=RateForm, formset=BaseRateFormSet, extra=0, max_num=18, can_delete=True)


def _rate_formset(data, group, default_terms):
    """Existing rates, plus empty rows for every channel/slot not used yet. The first row per
    channel is the simple one shown by default; the rest appear under “Add terms or time band”."""
    existing = list(group.rates.select_related("time_of_day")) if group.pk else []
    all_day = lambda t: t is None or (t.start_time is None and t.end_time is None)  # noqa: E731
    used = {(r.channel, r.slot) for r in existing if all_day(r.time_of_day)}
    initial = [{"channel": ch, "slot": slot, "payment_terms": default_terms, "rate_in": None, "rate_out": None}
               for slot in (1, 2, 3) for ch in ("sms", "voice") if (ch, slot) not in used]
    Fs = forms.inlineformset_factory(BillingGroup, BillingGroupRate, form=RateForm, formset=BaseRateFormSet,
                                     extra=len(initial), max_num=18, can_delete=True)
    fs = Fs(data, instance=group, initial=initial)
    first = set()
    for f in fs.forms:
        ch = f["channel"].value()
        slot = str(f["slot"].value())
        tod = f.instance.time_of_day if f.instance.pk else None
        f.is_simple = ch not in first and slot == "1" and (all_day(tod) if f.instance.pk else not f["time_of_day"].value())
        if f.is_simple:
            first.add(ch)
    return fs


def _group_qs():
    allocated = Allocation.objects.filter(number=OuterRef("pk"), ends_at__isnull=True)
    return BillingGroup.objects.select_related("supplier", "country", "currency", "number_type").annotate(
        n_total=Count("numbers", distinct=True),
        n_alloc=Count("numbers", filter=Q(numbers__allocations__isnull=False, numbers__allocations__ends_at__isnull=True), distinct=True),
    ).order_by("country__name", "title")


@staff_required
def group_list(request):
    qs = _group_qs()
    f = request.GET
    if f.get("supplier"):
        qs = qs.filter(supplier_id=f["supplier"])
    if f.get("country"):
        qs = qs.filter(country_id=f["country"])
    if f.get("routing"):
        qs = qs.filter(routing_type=f["routing"])
    if f.get("blocked") in ("0", "1"):
        qs = qs.filter(blocked=f["blocked"] == "1")
    if f.get("q"):
        qs = qs.filter(title__icontains=f["q"])
    if request.method == "POST":
        ids = request.POST.getlist("ids")
        action = request.POST.get("action")
        sel = BillingGroup.objects.filter(pk__in=ids)
        if action in ("block", "unblock"):
            n = sel.update(blocked=action == "block")
            messages.success(request, f"{action.title()}ed {n} billing groups.")
        return redirect(request.get_full_path())
    page = Paginator(qs, 50).get_page(f.get("page"))
    return render(request, "inventory/group_list.html", {
        "page": page, "nav": "groups", "f": f,
        "suppliers": Account.objects.filter(is_supplier=True),
        "countries": Country.objects.filter(billinggroup__isnull=False).distinct(),
    })


@staff_required
def group_edit(request, pk=None):
    obj = get_object_or_404(BillingGroup, pk=pk) if pk else None
    form = BillingGroupForm(request.POST or None, instance=obj,
                            initial={"supplier": request.GET.get("supplier")} if not obj else None)
    form.fields["supplier"].queryset = Account.objects.filter(is_supplier=True)
    for f, label in (("supplier", "Choose supplier"), ("country", "Choose country"), ("currency", "Choose currency"),
                     ("number_type", "Any")):
        form.fields[f].empty_label = label
    sup = Account.objects.filter(pk=request.GET.get("supplier") or 0).first() if not obj else None
    default_terms = (sup.payment_terms_id if sup and sup.payment_terms_id else
                     PaymentTerms.objects.filter(title="30/45").values_list("pk", flat=True).first())
    if sup and sup.currency_id and not obj:
        form.initial.setdefault("currency", sup.currency_id)
    rates = _rate_formset(request.POST or None, obj or BillingGroup(), default_terms)
    rates.routing_type = request.POST.get("routing_type") if request.method == "POST" else None
    from sms.models import BlockRule
    from sms import rules as R
    group_rules = R.rules_for(BlockRule.GROUP, group=obj).exclude(rule_type=BlockRule.LIMIT) if obj else []
    rules_form = R.QuickRulesForm(request.POST or None, prefix="rules",
                                  initial={"limit": R.initial_limit(BlockRule.GROUP, group=obj)} if obj else None)
    if request.method == "POST" and form.is_valid() and rates.is_valid() and rules_form.is_valid():
        g = form.save()
        rates.instance = g
        rates.save()
        BlockRule.objects.filter(scope=BlockRule.GROUP, billing_group=g,
                                 pk__in=request.POST.getlist("remove_rule")).delete()
        R.save_quick(rules_form, BlockRule.GROUP, group=g, reason="Set on billing group")
        from core.models import log
        log(request.user, "billing_group.save", g)
        messages.success(request, f"Saved billing group “{g.title}”.")
        return redirect("group_detail", g.pk)
    voice_fields = [form[n] for n in ("billing_increment_first", "billing_increment_next", "min_duration", "max_duration",
                                      "concurrent_limit", "acd", "asr")]
    return render(request, "inventory/group_form.html", {"form": form, "rates": rates, "obj": obj, "nav": "groups",
                                                         "voice_fields": voice_fields, "rules_form": rules_form,
                                                         "group_rules": group_rules})


@staff_required
def group_detail(request, pk):
    g = get_object_or_404(_group_qs(), pk=pk)
    return redirect(f"/numbers/?group={g.pk}")


class AddNumbersForm(forms.Form):
    MODES = [("range", "Range start + quantity"), ("list", "Paste a list"), ("csv", "Upload a CSV file")]
    billing_group = forms.ModelChoiceField(BillingGroup.objects.select_related("supplier"))
    mode = forms.ChoiceField(choices=MODES, widget=forms.RadioSelect, initial="range")
    start = forms.CharField(required=False, label="First number")
    quantity = forms.IntegerField(required=False, min_value=1, max_value=100000)
    numbers = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 6}),
                              help_text="One per line, or comma separated. Spaces and + are ignored.")
    file = forms.FileField(required=False, help_text="The first column that looks like a phone number is used.")
    other_country = forms.BooleanField(required=False, label="Allow numbers from another country")


@staff_required
def add_numbers(request):
    form = AddNumbersForm(request.POST or None, request.FILES or None,
                          initial={"billing_group": request.GET.get("group")})
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        group = d["billing_group"]
        dial = group.country.dial_code or ""
        skipped = []
        try:
            if d["mode"] == "range":
                if not d["quantity"]:
                    raise ValueError("Enter how many numbers are in the range.")
                nums = services.range_numbers(services.to_international([services.normalise(d["start"])], dial)[0],
                                              d["quantity"])
            else:
                if d["mode"] == "list":
                    text = d["numbers"]
                else:
                    if not d["file"]:
                        raise ValueError("Choose a CSV file.")
                    raw = d["file"].read()
                    try:
                        text = raw.decode("utf-8-sig")
                    except UnicodeDecodeError:
                        text = raw.decode("latin-1")
                services.check_text(text)
                nums = services.to_international(services.parse_number_list(text), dial)
                skipped = services.rejected_lines(text) if d["mode"] == "list" else []
            if not nums:
                raise ValueError("No phone numbers found. Numbers need 6 to 15 digits, with the country code.")
            foreign = [n for n in nums if dial and not n.startswith(dial)]
            if foreign and not d["other_country"]:
                raise ValueError(f"{len(foreign):,} of these don't start with +{dial} ({group.country.name}), "
                                 f"for example +{foreign[0]}. Check the billing group, or tick “Allow numbers "
                                 "from another country”.")
        except ValueError as e:
            form.add_error(None, str(e))
        else:
            res = services.add_numbers(d["billing_group"], nums, user=request.user)
            msg = f"Added {len(res.created):,} numbers to {d['billing_group'].title}."
            if res.duplicates:
                msg += f" Skipped {len(res.duplicates):,} that already exist."
            if skipped:
                msg += f" Ignored {len(skipped)} line{'s' if len(skipped) > 1 else ''} with no valid number " \
                       f"(e.g. “{skipped[0]}”)."
            messages.success(request, msg)
            return redirect(f"/numbers/?group={d['billing_group'].pk}")
    return render(request, "inventory/add_numbers.html", {"form": form, "nav": "numbers"})


def _numbers_qs(request):
    active = Allocation.objects.filter(number=OuterRef("pk"), ends_at__isnull=True)
    qs = Number.objects.select_related("billing_group__supplier", "billing_group__country").annotate(
        is_alloc=Exists(active))
    acc = client_account(request.user)
    if acc:
        qs = qs.filter(allocations__client=acc, allocations__ends_at__isnull=True)
    f = request.GET
    if f.get("group"):
        qs = qs.filter(billing_group_id=f["group"])
    if f.get("supplier"):
        qs = qs.filter(billing_group__supplier_id=f["supplier"])
    if f.get("country"):
        qs = qs.filter(billing_group__country_id=f["country"])
    if f.get("client"):
        qs = qs.filter(allocations__client_id=f["client"], allocations__ends_at__isnull=True)
    status = f.get("status")
    if status == "free":
        qs = qs.filter(is_alloc=False, blocked=False)
    elif status == "allocated":
        qs = qs.filter(is_alloc=True)
    elif status == "blocked":
        qs = qs.filter(blocked=True)
    if f.get("routing") == "sms":
        qs = qs.filter(billing_group__routing_type__in=["sms", "both"])
    elif f.get("routing") == "voice":
        qs = qs.filter(billing_group__routing_type__in=["voice", "both"])
    if f.get("q"):
        qs = qs.filter(number__contains=services.normalise(f["q"]))
    return qs.distinct()


def number_list(request):
    if not request.user.is_authenticated:
        return redirect("login")
    qs = _numbers_qs(request)
    if request.GET.get("export") == "csv":
        return _export_numbers(qs)
    per = request.GET.get("per", "50")
    per = min(max(int(per), 10), 500) if per.isdigit() else 50
    page = Paginator(qs, per).get_page(request.GET.get("page"))
    allocs = {}
    for a in (Allocation.objects.filter(number__in=page.object_list, ends_at__isnull=True)
              .select_related("client", "payment_terms").order_by("channel")):
        allocs.setdefault(a.number_id, []).append(a)
    rows = [(n, allocs.get(n.pk, [])) for n in page.object_list]
    stats = Number.objects.aggregate(total=Count("pk"), blocked=Count("pk", filter=Q(blocked=True)))
    stats["allocated"] = Allocation.objects.filter(ends_at__isnull=True).values("number").distinct().count()
    stats["free"] = stats["total"] - stats["allocated"] - stats["blocked"]
    return render(request, "inventory/number_list.html", {
        "page": page, "rows": rows, "f": request.GET, "nav": "numbers", "stats": stats,
        "is_client": bool(client_account(request.user)),
        "groups": BillingGroup.objects.select_related("supplier"),
        "suppliers": Account.objects.filter(is_supplier=True),
        "clients": Account.objects.filter(is_client=True),
        "countries": Country.objects.filter(billinggroup__isnull=False).distinct(),
    })


def _export_numbers(qs):
    resp = HttpResponse(content_type="text/csv")
    resp["Content-Disposition"] = 'attachment; filename="numbers.csv"'
    w = csv.writer(resp)
    w.writerow(["number", "country", "supplier", "billing_group", "usable_for", "status",
                "sms_client", "voice_client", "terms", "date_allocated"])
    allocs = {}
    for a in Allocation.objects.filter(ends_at__isnull=True).select_related("client", "payment_terms"):
        allocs.setdefault(a.number_id, {})[a.channel] = a
    for n in qs.iterator(chunk_size=2000):
        a = allocs.get(n.pk, {})
        first = a.get("sms") or a.get("voice")
        w.writerow([n.number, n.billing_group.country, n.billing_group.supplier, n.billing_group.title,
                    n.billing_group.get_routing_type_display(),
                    "blocked" if n.blocked else ("allocated" if a else "free"),
                    a["sms"].client if "sms" in a else "", a["voice"].client if "voice" in a else "",
                    first.payment_terms or "" if first else "", first.starts_at.date() if first else ""])
    return resp


CHANNEL_CHOICES = [("both", "SMS & voice (whatever each number supports)"), ("sms", "SMS only"),
                   ("voice", "Voice only")]


def _channels(value):
    return None if value in ("", "both", None) else [value]


class AllocateForm(forms.Form):
    client = forms.ModelChoiceField(Account.objects.filter(is_client=True))
    use_for = forms.ChoiceField(choices=CHANNEL_CHOICES, initial="both", label="Use for")
    payment_terms = forms.ModelChoiceField(PaymentTerms.objects.all(), required=False)
    all_terms = forms.BooleanField(required=False)
    rate_out = forms.DecimalField(required=False, max_digits=10, decimal_places=5,
                                  help_text="Client payout per SMS (or per minute for voice). Leave blank to use the billing group's default for each channel.")
    time_of_day = forms.ModelChoiceField(TimeOfDay.objects.all(), required=False)
    service = forms.CharField(required=False, help_text="Voice only")


def _carry(request):
    """The selection (ids or 'all matching' + filters) to re-post on the confirm step."""
    keep = [("ids", v) for v in request.POST.getlist("ids")]
    keep += [(k, v) for k, v in request.POST.items() if k.startswith("f_") or k == "select_all_matching"]
    return keep


@staff_required
def bulk_action(request):
    if request.method != "POST":
        return redirect("number_list")
    ids = request.POST.getlist("ids")
    select_all = request.POST.get("select_all_matching") == "1"
    action = request.POST.get("action")
    back = request.POST.get("back") or "/numbers/"
    if select_all:
        request.GET = request.GET.copy()
        for k in ("group", "supplier", "country", "client", "status", "routing", "q"):
            if request.POST.get("f_" + k):
                request.GET[k] = request.POST["f_" + k]
        numbers = list(_numbers_qs(request))
    else:
        numbers = list(Number.objects.filter(pk__in=ids).select_related("billing_group"))
    if not numbers:
        messages.error(request, "Select at least one number.")
        return redirect(back)

    if action == "allocate":
        from sms.models import BlockRule
        from sms import rules as R
        confirm = bool(request.POST.get("confirm"))
        form = AllocateForm(request.POST if confirm else None)
        rules_form = R.QuickRulesForm(request.POST if confirm else None, prefix="rules")
        if form.is_valid() and rules_form.is_valid():
            d = form.cleaned_data
            rates = None
            if d["rate_out"] is not None:
                rates = [dict(slot=1, payment_terms=d["payment_terms"], time_of_day=d["time_of_day"],
                              rate_out=d["rate_out"])]
            created, moved = services.allocate(numbers, d["client"], d["payment_terms"], d["all_terms"], rates,
                                               d["service"], channels=_channels(d["use_for"]), user=request.user)
            msg = f"Allocated {created:,} numbers to {d['client']}."
            if moved:
                msg += f" {moved:,} were moved from another client."
            if any(rules_form.cleaned_data.get(k) for k in ("limit", "senders", "prefixes")):
                rules_form.cleaned_data.setdefault("limit", None)
                if not rules_form.cleaned_data["limit"]:  # blank here means "leave the client's limit as it is"
                    rules_form.cleaned_data["limit"] = R.initial_limit(BlockRule.CLIENT, account=d["client"])
                n = R.save_quick(rules_form, BlockRule.CLIENT, account=d["client"], reason="Set when allocating")
                if n:
                    msg += f" Updated {n} blocking rule{'s' if n != 1 else ''} for {d['client']}."
            messages.success(request, msg)
            return redirect(back)
        already = sum(1 for n in numbers if n.current_allocation)
        client_rules = {}
        for r in BlockRule.objects.filter(scope=BlockRule.CLIENT).select_related("account"):
            client_rules.setdefault(r.account_id, []).append(str(r))
        return render(request, "inventory/allocate.html", {
            "rules_form": rules_form, "client_rules": client_rules,
            "form": form, "numbers": numbers[:20], "count": len(numbers), "already": already,
            "carry": _carry(request), "back": back, "nav": "numbers"})

    if action in ("unallocate", "block", "unblock", "delete", "move") and not request.POST.get("confirm"):
        return render(request, "inventory/confirm.html", {
            "action": action, "count": len(numbers), "numbers": numbers[:20], "carry": _carry(request),
            "back": back, "nav": "numbers", "groups": BillingGroup.objects.select_related("supplier"),
            "channel_choices": CHANNEL_CHOICES})

    if action == "unallocate":
        n = services.unallocate(numbers, channels=_channels(request.POST.get("use_for")), user=request.user)
        messages.success(request, f"Unallocated {n:,} numbers.")
    elif action in ("block", "unblock"):
        n = services.set_blocked(numbers, action == "block", user=request.user)
        messages.success(request, f"{action.title()}ed {n:,} numbers.")
    elif action == "move":
        g = get_object_or_404(BillingGroup, pk=request.POST.get("target_group"))
        n = services.move_to_group(numbers, g, user=request.user)
        messages.success(request, f"Moved {n:,} numbers to {g.title}.")
    elif action == "delete":
        deleted, kept = services.delete_numbers(numbers, user=request.user)
        msg = f"Deleted {deleted:,} numbers."
        if kept:
            msg += f" Kept {kept:,} that have SMS history (block them instead)."
        messages.success(request, msg)
    return redirect(back)
