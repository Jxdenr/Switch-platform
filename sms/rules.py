"""Blocking rules and limits set from the places they belong: a billing group, an allocation
(the client's route) or the Blocking rules page."""
import re

from django import forms

from .models import BlockRule


def _split(text):
    return [v for v in (x.strip() for x in re.split(r"[\n,;]+", text or "")) if v]


class QuickRulesForm(forms.Form):
    limit = forms.IntegerField(required=False, min_value=1, label="Max SMS per number per hour",
                               help_text="Stops floods to one number (SMS pumping). Blank = no limit.")
    senders = forms.CharField(required=False, label="Block sender IDs",
                              widget=forms.Textarea(attrs={"rows": 2, "placeholder": "e.g. SpamCo, 12345"}),
                              help_text="One per line or comma separated.")
    prefixes = forms.CharField(required=False, label="Block number prefixes",
                               widget=forms.Textarea(attrs={"rows": 2, "placeholder": "e.g. 447700900"}),
                               help_text="Destination numbers starting with these, in international format.")

    def clean_prefixes(self):
        vals = [re.sub(r"[\s+]", "", v) for v in _split(self.cleaned_data["prefixes"])]
        bad = [v for v in vals if not v.isdigit()]
        if bad:
            raise forms.ValidationError(f"Prefixes must be digits: {', '.join(bad[:3])}")
        return vals

    def clean_senders(self):
        return _split(self.cleaned_data["senders"])


def rules_for(scope, account=None, group=None):
    qs = BlockRule.objects.filter(scope=scope)
    if scope == BlockRule.GROUP:
        return qs.filter(billing_group=group)
    if scope in (BlockRule.SUPPLIER, BlockRule.CLIENT):
        return qs.filter(account=account)
    return qs


def initial_limit(scope, account=None, group=None):
    r = rules_for(scope, account, group).filter(rule_type=BlockRule.LIMIT).first()
    return int(r.value) if r and r.value.isdigit() else None


def save_quick(form, scope, account=None, group=None, reason=""):
    """Apply a QuickRulesForm: the limit replaces any existing one (blank removes it); sender IDs and
    prefixes are added if not already there. Returns the number of rules added or changed."""
    d = form.cleaned_data
    existing = rules_for(scope, account, group)
    target = {"scope": scope, "account": account if scope != BlockRule.GROUP else None,
              "billing_group": group if scope == BlockRule.GROUP else None}
    changed = 0
    limit = existing.filter(rule_type=BlockRule.LIMIT).first()
    if d.get("limit"):
        if not limit:
            BlockRule.objects.create(rule_type=BlockRule.LIMIT, value=str(d["limit"]), reason=reason, **target)
            changed += 1
        elif limit.value != str(d["limit"]):
            limit.value = str(d["limit"])
            limit.save(update_fields=["value"])
            changed += 1
    elif limit:
        limit.delete()
        changed += 1
    for kind, values in ((BlockRule.SENDER, d.get("senders") or []), (BlockRule.PREFIX, d.get("prefixes") or [])):
        for v in values:
            if not existing.filter(rule_type=kind, value__iexact=v).exists():
                BlockRule.objects.create(rule_type=kind, value=v[:40], reason=reason, **target)
                changed += 1
    return changed
