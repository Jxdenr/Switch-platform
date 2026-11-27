"""Sign in (password, then authenticator code if turned on), forgotten password, invites,
and each user's own security page.

Web logins are separate from SMPP and HTTP bind credentials: those live on the CRM account
(crm.SmppUser, crm.HttpUser, wholesale.ApiKey), only work on the SMPP port or the API, and can
never be used to sign in here. A web login can never bind over SMPP either.
"""
from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, get_user_model, login, update_session_auth_hash
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_not_required
from django.contrib.auth.forms import PasswordChangeForm, PasswordResetForm, SetPasswordForm
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import url_has_allowed_host_and_scheme, urlsafe_base64_encode
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_POST

from . import security, totp
from .models import log

LOCKED = "Too many attempts. Wait 15 minutes and try again."


class LoginForm(forms.Form):
    username = forms.CharField(label="Email or username", max_length=150,
                               widget=forms.TextInput(attrs={"autofocus": True, "autocomplete": "username"}))
    password = forms.CharField(widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}))


class CodeForm(forms.Form):
    code = forms.CharField(label="6-digit code from your authenticator app", max_length=10, widget=forms.TextInput(
        attrs={"autofocus": True, "autocomplete": "one-time-code", "inputmode": "numeric", "pattern": "[0-9 ]*"}))


def _next(request):
    nxt = request.POST.get("next") or request.GET.get("next") or ""
    if url_has_allowed_host_and_scheme(nxt, {request.get_host()}, require_https=request.is_secure()):
        return nxt
    return reverse(settings.LOGIN_REDIRECT_URL)


def _home(user):
    return reverse("platform") if user.is_superuser else reverse(settings.LOGIN_REDIRECT_URL)


def _finish(request, user):
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    security.record(user.username, security.client_ip(request), True,
                    "code" if getattr(user.profile, "totp_enabled", False) else "password")
    log(user, "login", user.username, tenant=getattr(user.profile, "tenant_id", None),
        ip=security.client_ip(request))
    nxt = _next(request)
    return redirect(_home(user) if nxt == reverse(settings.LOGIN_REDIRECT_URL) else nxt)


@login_not_required
@sensitive_post_parameters("password")
@never_cache
def login_view(request):
    if request.user.is_authenticated:
        return redirect(_home(request.user))
    form = LoginForm(request.POST or None)
    ip = security.client_ip(request)
    if request.method == "POST" and form.is_valid():
        name = form.cleaned_data["username"].strip()
        if security.locked(name, ip):
            form.add_error(None, LOCKED)
        else:
            user = _authenticate(request, name, form.cleaned_data["password"])
            if not user:
                security.record(name, ip, False)
                form.add_error(None, "That email or password isn't right.")
            elif user.profile.totp_enabled:
                request.session.cycle_key()
                request.session["pending_user"] = user.pk
                request.session["pending_next"] = _next(request)
                return redirect("login_code")
            else:
                return _finish(request, user)
    return render(request, "core/login.html", {"form": form, "next": request.GET.get("next", "")})


def _authenticate(request, name, password):
    User = get_user_model()
    # Sign in with the email address or the username; usernames are emails for new logins.
    match = User.objects.filter(email__iexact=name).first() if "@" in name else None
    return authenticate(request, username=match.username if match else name, password=password)


@login_not_required
@never_cache
def login_code(request):
    User = get_user_model()
    user = User.objects.filter(pk=request.session.get("pending_user"), is_active=True).first()
    if not user:
        return redirect("login")
    form = CodeForm(request.POST or None)
    ip = security.client_ip(request)
    if request.method == "POST" and form.is_valid():
        if security.locked(user.username, ip):
            form.add_error(None, LOCKED)
        else:
            p = user.profile
            step = totp.verify(p.totp_secret, form.cleaned_data["code"], p.totp_last_step)
            if step is None:
                security.record(user.username, ip, False, "code")
                form.add_error("code", "That code isn't right, or it was already used. Try the next one.")
            else:
                type(p).objects.filter(pk=p.pk).update(totp_last_step=step)
                nxt = request.session.pop("pending_next", "")
                request.session.pop("pending_user", None)
                request.POST = request.POST.copy()
                request.POST["next"] = nxt
                return _finish(request, user)
    return render(request, "core/login_code.html", {"form": form})


class ResetForm(PasswordResetForm):
    email = forms.EmailField(label="Your email", max_length=254)


@login_not_required
def password_reset(request):
    ip = security.client_ip(request)
    if request.method == "POST" and security.locked("", ip):
        messages.error(request, LOCKED)
        return redirect("password_reset")
    if request.method == "POST":
        security.record("reset:" + request.POST.get("email", ""), ip, False, "reset")  # counts towards the IP limit
    return auth_views.PasswordResetView.as_view(
        form_class=ResetForm, template_name="core/password_reset.html",
        email_template_name="core/password_reset_email.txt", subject_template_name="core/password_reset_subject.txt",
        success_url=reverse("login") + "?reset=sent")(request)


class NewPasswordForm(SetPasswordForm):
    pass


set_password = login_not_required(auth_views.PasswordResetConfirmView.as_view(
    form_class=NewPasswordForm, template_name="core/set_password.html", success_url="/login/?reset=done",
    post_reset_login=False))


def invite_link(request, user):
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    return request.build_absolute_uri(reverse("set_password", args=[uid, token]))


def send_invite(request, user, portal_name):
    """Email a 'set your password' link. Returns the link so the page can show it when email
    isn't set up (EMAIL_HOST empty) or fails."""
    link = invite_link(request, user)
    days = settings.PASSWORD_RESET_TIMEOUT // 86400
    body = (f"Hello {user.first_name or ''},\n\nA login has been created for you on {portal_name}.\n\n"
            f"Choose your password here (the link works once and expires in {days} days):\n{link}\n\n"
            f"Your sign-in email is {user.email}.\n")
    sent = False
    if user.email:
        try:
            sent = bool(send_mail(f"Your login for {portal_name}", body, settings.DEFAULT_FROM_EMAIL, [user.email]))
        except Exception as e:  # noqa: BLE001 - shown to the person creating the login
            messages.error(request, f"Couldn't email the invite ({type(e).__name__}). Copy the link below instead.")
    return link, sent and bool(settings.EMAIL_HOST)


# --- Each user's own security page -----------------------------------------------------------

@never_cache
def security_page(request):
    p = request.user.profile
    if not p.totp_secret or (not p.totp_enabled and request.GET.get("new")):
        p.totp_secret = totp.new_secret()
        p.save(update_fields=["totp_secret"])
    code_form = CodeForm(request.POST if request.POST.get("action") == "enable" else None)
    pw_form = PasswordChangeForm(request.user, request.POST if request.POST.get("action") == "password" else None)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "enable" and code_form.is_valid():
            step = totp.verify(p.totp_secret, code_form.cleaned_data["code"], p.totp_last_step)
            if step is None:
                code_form.add_error("code", "That code didn't match. Check the time on your phone and try again.")
            else:
                p.totp_enabled, p.totp_last_step = True, step
                p.save(update_fields=["totp_enabled", "totp_last_step"])
                log(request.user, "2fa.enable", request.user.username, tenant=p.tenant_id)
                messages.success(request, "Two-factor sign-in is on. You'll need a code each time you sign in.")
                return redirect("security")
        elif action == "disable":
            if security.needs_2fa(request.user):
                messages.error(request, "Your login must use two-factor sign-in, so it can't be turned off.")
            elif not request.user.check_password(request.POST.get("password", "")):
                messages.error(request, "Your password wasn't right, so two-factor is still on.")
            else:
                p.totp_enabled, p.totp_secret = False, ""
                p.save(update_fields=["totp_enabled", "totp_secret"])
                log(request.user, "2fa.disable", request.user.username, tenant=p.tenant_id)
                messages.success(request, "Two-factor sign-in is off.")
            return redirect("security")
        elif action == "password" and pw_form.is_valid():
            pw_form.save()
            update_session_auth_hash(request, pw_form.user)  # stay signed in here, sign out elsewhere
            log(request.user, "password.change", request.user.username, tenant=p.tenant_id)
            messages.success(request, "Password changed. Other devices have been signed out.")
            return redirect("security")
    issuer = (p.tenant.name if p.tenant else "Switch platform")
    ctx = {"p": p, "code_form": code_form, "pw_form": pw_form, "required": security.needs_2fa(request.user),
           "setup": request.GET.get("setup"), "nav": "security"}
    if not p.totp_enabled:
        link = totp.uri(p.totp_secret, request.user.email or request.user.username, issuer)
        ctx.update(qr=totp.qr_svg(link), secret=" ".join(p.totp_secret[i:i + 4] for i in range(0, len(p.totp_secret), 4)))
    return render(request, "core/security.html", ctx)


@require_POST
def logout_view(request):
    from django.contrib.auth import logout
    logout(request)
    return redirect("login")
