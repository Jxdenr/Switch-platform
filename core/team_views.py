"""Logins: a portal owner manages the portal's team and client logins; the platform admin
creates, brands and suspends portals and invites each portal's owner."""
from django import forms
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import tenancy
from .access import owner_required, platform_admin_required
from .auth_views import send_invite
from .models import SECTIONS, Profile, Tenant, log

User = get_user_model()
COLOURWAYS = [("harbour", "Harbour (teal)"), ("graphite", "Graphite (amber)"), ("paper", "Paper (blue)")]


class PersonForm(forms.Form):
    first_name = forms.CharField(max_length=150)
    last_name = forms.CharField(max_length=150, required=False)
    email = forms.EmailField(help_text="They sign in with this. We email them a link to choose their own password.")

    def __init__(self, *a, editing=None, **kw):
        self.editing = editing
        super().__init__(*a, **kw)

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        clash = User.objects.filter(Q(email__iexact=email) | Q(username__iexact=email))
        if self.editing:
            clash = clash.exclude(pk=self.editing.pk)
        if clash.exists():
            raise forms.ValidationError("That email already has a login. Each person needs their own email.")
        return email


class LoginForm(PersonForm):
    KINDS = [(Profile.ROLE_MEMBER, "Team member: sees the areas you choose"),
             (Profile.ROLE_CLIENT, "Client: sees only their own account (client portal)"),
             (Profile.ROLE_OWNER, "Owner: sees and changes everything, including logins")]
    role = forms.ChoiceField(label="Kind of login", choices=KINDS, widget=forms.RadioSelect,
                             initial=Profile.ROLE_MEMBER)
    sections = forms.MultipleChoiceField(
        label="Areas they can see", required=False, widget=forms.CheckboxSelectMultiple,
        choices=[(k, f"{lbl}: {desc}") for k, lbl, desc in SECTIONS],
        help_text="Team members only. The dashboard is always visible; money figures on it need Money.")
    can_edit = forms.BooleanField(label="Can make changes (otherwise view only)", required=False)
    account = forms.ModelChoiceField(label="Client account", queryset=None, required=False,
                                     help_text="Client logins only: the account whose data they see.")

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        from crm.models import Account
        self.fields["account"].queryset = Account.objects.filter(is_client=True)  # this portal only

    def clean(self):
        d = super().clean()
        if d.get("role") == Profile.ROLE_CLIENT and not d.get("account"):
            self.add_error("account", "Choose which client account this login sees.")
        if d.get("role") == Profile.ROLE_MEMBER and not d.get("sections"):
            self.add_error("sections", "Choose at least one area, or they'll only see an empty dashboard.")
        return d


def _create_login(request, tenant, data, role, **profile):
    with transaction.atomic():
        u = User(username=data["email"], email=data["email"], first_name=data["first_name"],
                 last_name=data.get("last_name", ""))
        u.set_unusable_password()
        u.save()
        Profile.objects.filter(user=u).update(tenant=tenant, role=role, invited_by=request.user, **profile)
        u.refresh_from_db()
    log(request.user, "login.create", u.email, tenant=tenant, role=role)
    link, emailed = send_invite(request, u, tenant.name)
    return u, link, emailed


def _show_link(request, user, link, emailed):
    if emailed:
        messages.success(request, f"Invite emailed to {user.email}.")
    else:
        request.session["invite_link"] = {"email": user.email, "link": link}


def _pop_link(request):
    return request.session.pop("invite_link", None)


# --- Portal owner: team and client logins ----------------------------------------------------

@owner_required
def team(request):
    t = request.tenant
    people = (Profile.objects.filter(tenant=t).select_related("user", "account")
              .order_by("role", "user__first_name", "user__email"))
    return render(request, "core/team.html", {
        "people": people, "nav": "team", "invite": _pop_link(request), "sections": dict(
            (k, lbl) for k, lbl, _ in SECTIONS)})


@owner_required
def team_edit(request, pk=None):
    t = request.tenant
    p = get_object_or_404(Profile.objects.select_related("user"), pk=pk, tenant=t) if pk else None
    initial = {}
    if p:
        initial = {"first_name": p.user.first_name, "last_name": p.user.last_name, "email": p.user.email,
                   "role": p.role, "sections": p.sections, "can_edit": p.can_edit, "account": p.account_id}
    form = LoginForm(request.POST or None, initial=initial, editing=p.user if p else None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        fields = {"sections": d["sections"] if d["role"] == Profile.ROLE_MEMBER else [],
                  "can_edit": d["can_edit"] if d["role"] == Profile.ROLE_MEMBER else False,
                  "account": d["account"] if d["role"] == Profile.ROLE_CLIENT else None}
        if p:
            if p.user == request.user and d["role"] != Profile.ROLE_OWNER:
                messages.error(request, "You can't change your own login away from owner. Ask another owner.")
                return redirect("team")
            u = p.user
            changed_email = u.email != d["email"]
            u.first_name, u.last_name, u.email, u.username = d["first_name"], d["last_name"], d["email"], d["email"]
            u.save()
            Profile.objects.filter(pk=p.pk).update(role=d["role"], **fields)
            log(request.user, "login.update", u.email, role=d["role"])
            messages.success(request, f"Saved {u.email}." + (" They now sign in with the new email." if changed_email else ""))
        else:
            u, link, emailed = _create_login(request, t, d, d["role"], **fields)
            _show_link(request, u, link, emailed)
        return redirect("team")
    return render(request, "core/team_form.html", {"form": form, "p": p, "nav": "team"})


@owner_required
@require_POST
def team_action(request, pk):
    p = get_object_or_404(Profile.objects.select_related("user"), pk=pk, tenant=request.tenant)
    return _account_action(request, p, "team")


def _account_action(request, p, back, **kw):
    u, action = p.user, request.POST.get("action")
    if action in ("disable", "enable"):
        if u == request.user:
            messages.error(request, "You can't disable your own login.")
            return redirect(back, **kw)
        if action == "disable" and p.role == Profile.ROLE_OWNER and not Profile.objects.filter(
                tenant=p.tenant, role=Profile.ROLE_OWNER, user__is_active=True).exclude(pk=p.pk).exists():
            messages.error(request, "That's the portal's only owner. Make someone else an owner first.")
            return redirect(back, **kw)
        u.is_active = action == "enable"
        u.save(update_fields=["is_active"])
        messages.success(request, f"{u.email} {'can sign in again' if u.is_active else 'can no longer sign in'}.")
    elif action == "invite":
        link, emailed = send_invite(request, u, p.tenant.name)
        _show_link(request, u, link, emailed)
    elif action == "reset_2fa":
        Profile.objects.filter(pk=p.pk).update(totp_enabled=False, totp_secret="", totp_last_step=0)
        messages.success(request, f"Two-factor turned off for {u.email}. They can set it up again on their Security page.")
    log(request.user, f"login.{action}", u.email, tenant=p.tenant_id)
    return redirect(back, **kw)


class PortalSettingsForm(forms.ModelForm):
    class Meta:
        model = Tenant
        fields = ["name", "company_name", "company_details", "billing_email", "default_currency", "colourway"]
        labels = {"name": "Portal name", "company_name": "Company legal name",
                  "company_details": "Company details for statements and invoices",
                  "billing_email": "Billing reply-to email", "colourway": "Colour scheme"}
        widgets = {"company_details": forms.Textarea(attrs={"rows": 6, "placeholder": (
            "Address\nCompany number, VAT number\nBank: name, account name, sort code, account number, IBAN, SWIFT")}),
            "colourway": forms.Select(choices=COLOURWAYS)}


@owner_required
def portal_settings(request):
    form = PortalSettingsForm(request.POST or None, instance=request.tenant)
    if request.method == "POST" and form.is_valid():
        form.save()
        log(request.user, "portal.settings", request.tenant)
        messages.success(request, "Saved. New statements and invoices use these details.")
        return redirect("portal_settings")
    return render(request, "core/portal_settings.html", {"form": form, "nav": "portal_settings"})


# --- Platform admin: portals -----------------------------------------------------------------

class NewPortalForm(PersonForm):
    portal = forms.CharField(label="Portal name", max_length=120, help_text="Usually the customer's company name.")
    colourway = forms.ChoiceField(label="Colour scheme", choices=COLOURWAYS)

    field_order = ["portal", "colourway", "first_name", "last_name", "email"]

    def clean_portal(self):
        name = self.cleaned_data["portal"].strip()
        if Tenant.objects.filter(name__iexact=name).exists():
            raise forms.ValidationError("There's already a portal with that name.")
        return name


class PortalForm(forms.ModelForm):
    class Meta:
        model = Tenant
        fields = ["name", "active", "colourway", "company_name"]
        labels = {"active": "Active (untick to suspend: nobody in it can sign in)"}
        widgets = {"colourway": forms.Select(choices=COLOURWAYS)}


@platform_admin_required
def platform(request):
    portals = Tenant.objects.annotate(
        n_logins=Count("profiles", filter=Q(profiles__user__is_active=True)),
        n_owners=Count("profiles", filter=Q(profiles__role=Profile.ROLE_OWNER))).order_by("name")
    form = NewPortalForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        t = Tenant.objects.create(name=d["portal"], company_name=d["portal"], colourway=d["colourway"])
        u, link, emailed = _create_login(request, t, d, Profile.ROLE_OWNER)
        log(request.user, "portal.create", t, tenant=t)
        messages.success(request, f"Created {t}. It's empty until {u.email} signs in and sets it up.")
        _show_link(request, u, link, emailed)
        return redirect("platform")
    return render(request, "core/platform.html", {"portals": portals, "form": form, "invite": _pop_link(request),
                                                  "nav": "platform"})


@platform_admin_required
def platform_portal(request, pk):
    t = get_object_or_404(Tenant, pk=pk)
    form = PortalForm(request.POST or None, instance=t, prefix="p")
    owner_form = PersonForm(request.POST or None, prefix="o")
    if request.method == "POST":
        if request.POST.get("action") == "save" and form.is_valid():
            form.save()
            log(request.user, "portal.update", t, tenant=t, active=t.active)
            messages.success(request, f"Saved {t}." + ("" if t.active else " It's suspended: nobody in it can sign in."))
            return redirect("platform_portal", pk=t.pk)
        if request.POST.get("action") == "add_owner" and owner_form.is_valid():
            u, link, emailed = _create_login(request, t, owner_form.cleaned_data, Profile.ROLE_OWNER)
            _show_link(request, u, link, emailed)
            return redirect("platform_portal", pk=t.pk)
    owners = Profile.objects.filter(tenant=t, role=Profile.ROLE_OWNER).select_related("user")
    with tenancy.use_tenant(t):  # counts only, never the data itself
        from crm.models import Account
        from inventory.models import Number
        size = {"accounts": Account.objects.count(), "numbers": Number.objects.count()}
    return render(request, "core/platform_portal.html", {
        "t": t, "form": form, "owner_form": owner_form, "owners": owners, "size": size,
        "n_logins": Profile.objects.filter(tenant=t).count(), "invite": _pop_link(request), "nav": "platform"})


@platform_admin_required
@require_POST
def platform_owner_action(request, pk, profile_pk):
    p = get_object_or_404(Profile.objects.select_related("user", "tenant"), pk=profile_pk, tenant_id=pk,
                          role=Profile.ROLE_OWNER)
    return _account_action(request, p, "platform_portal", pk=pk)
