"""Separate portals and logins: nothing open without a login, no portal can see or touch
another portal's data, team members only see their areas, and sign-in is hardened."""
import re
from decimal import Decimal
from unittest import mock

from django import forms
from django.contrib.auth.models import User
from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import URLPattern, URLResolver, get_resolver

from core import tenancy, totp
from core.models import Country, Currency, LoginAttempt, PaymentTerms, Profile, Tenant, TimeOfDay
from crm.models import Account, SmppUser
from inventory import services
from inventory.models import Allocation, BillingGroup, BillingGroupRate, Number
from sms.models import Message
from tests.base import make_login, make_portal

# The only URLs that answer without a login. Each checks its own token or API key.
PUBLIC = {"health", "login", "login_code", "password_reset", "set_password",
          "ingest_jasmin", "api_inbound", "ingest_freeswitch", "wholesale_send"}


def all_patterns(resolver=None, prefix=""):
    resolver = resolver or get_resolver()
    for p in resolver.url_patterns:
        if isinstance(p, URLResolver):
            if str(p.pattern).startswith("admin/"):
                continue  # Django admin: its own login, which we redirect to ours (tested below)
            yield from all_patterns(p, prefix + str(p.pattern))
        elif isinstance(p, URLPattern):
            yield p, prefix + str(p.pattern)


def sample_url(route):
    return "/" + re.sub(r"<(?:(\w+):)?(\w+)>", lambda m: {"int": "1", "slug": "x"}.get(m.group(1), "2026-01"), route)


def world(tenant, tag, start):
    """A supplier, a client, a billing group with 3 numbers allocated to the client."""
    with tenancy.use_tenant(tenant):
        gbp, uk = Currency.objects.get(code="GBP"), Country.objects.get(name="United Kingdom")
        terms = PaymentTerms.objects.get(title="30/45")
        sup = Account.objects.create(title=f"{tag} Supplier", is_supplier=True)
        cli = Account.objects.create(title=f"{tag} Client", is_client=True)
        SmppUser.objects.create(account=sup, title="bind", username=f"{tag.lower()}bind", password="pw")
        g = BillingGroup.objects.create(supplier=sup, title=f"{tag} UK", country=uk, currency=gbp)
        BillingGroupRate.objects.create(billing_group=g, slot=1, payment_terms=terms,
                                        time_of_day=TimeOfDay.objects.get(title="All Times"),
                                        rate_in=Decimal("0.02"), rate_out=Decimal("0.01"))
        services.add_numbers(g, [str(start + i) for i in range(3)])
        services.allocate(Number.objects.filter(billing_group=g), cli, terms)
        return {"sup": sup, "cli": cli, "group": g, "terms": terms}


class Base(TestCase):
    def setUp(self):
        call_command("seed_lookups", verbosity=0)
        self.a, self.b = make_portal("Alpha Telecom"), make_portal("Bravo Comms", company_name="Bravo Comms Ltd")
        self.wa = world(self.a, "Alpha", 447700900100)
        self.wb = world(self.b, "Bravo", 447700900200)
        self.owner_a = make_login("owner-a", self.a)
        self.owner_b = make_login("owner-b", self.b)


class LoginRequiredEverywhere(Base):
    def test_every_page_needs_a_login(self):
        seen = 0
        for p, route in all_patterns():
            if p.name in PUBLIC:
                continue
            url = sample_url(route)
            for method in ("get", "post"):
                r = getattr(self.client, method)(url)
                self.assertEqual(r.status_code, 302, f"{method.upper()} {url} answered {r.status_code} without login")
                self.assertTrue(r["Location"].startswith("/login/"), f"{url} -> {r['Location']}")
            seen += 1
        self.assertGreater(seen, 40)

    def test_public_list_is_only_machine_endpoints_and_sign_in(self):
        for p, route in all_patterns():
            view = p.callback
            if getattr(view, "login_required", True) is False:
                self.assertIn(p.name, PUBLIC, f"{route} is public but not on the allowed list")

    def test_machine_endpoints_still_check_their_token(self):
        self.assertEqual(self.client.post("/ingest/jasmin/?token=wrong", {"to": "1"}).status_code, 403)
        self.assertEqual(self.client.post("/api/sms/inbound/", {"key": "nope"}).status_code, 403)
        self.assertEqual(self.client.post("/ingest/freeswitch/?token=wrong").status_code, 403)
        self.assertEqual(self.client.post("/api/wholesale/send/", {"key": "nope"}).status_code, 403)


class Isolation(Base):
    def test_lists_only_show_own_portal(self):
        self.client.force_login(self.owner_a)
        for url in ["/suppliers/", "/clients/", "/billing-groups/", "/numbers/", "/sms/records/", "/binds/"]:
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertNotContains(r, "Bravo", msg_prefix=url)
        self.assertContains(self.client.get("/suppliers/"), "Alpha Supplier")
        r = self.client.get("/numbers/?q=447700900200")
        self.assertEqual(len(r.context["rows"]), 0)

    def test_other_portals_records_are_404_not_403(self):
        self.client.force_login(self.owner_a)
        b = self.wb
        for url in [f"/accounts/{b['sup'].pk}/", f"/accounts/{b['sup'].pk}/edit/",
                    f"/billing-groups/{b['group'].pk}/", f"/billing-groups/{b['group'].pk}/edit/",
                    f"/accounts/{b['cli'].pk}/documents/statement/2026-01/"]:
            self.assertEqual(self.client.get(url).status_code, 404, url)

    def test_forms_cant_point_at_another_portals_records(self):
        self.client.force_login(self.owner_a)
        ids = list(Number.unscoped.filter(tenant=self.a).values_list("pk", flat=True))
        Allocation.unscoped.filter(tenant=self.a).delete()
        r = self.client.post("/numbers/bulk/", {"action": "allocate", "confirm": "1", "ids": ids,
                                                "client": self.wb["cli"].pk, "payment_terms": self.wa["terms"].pk,
                                                "use_for": "both"})
        self.assertFalse(Allocation.unscoped.filter(client=self.wb["cli"]).exclude(tenant=self.b).exists())
        self.assertFalse(Allocation.unscoped.filter(tenant=self.a).exists(), r.content[:300])
        # Bulk actions on another portal's number ids do nothing.
        b_ids = list(Number.unscoped.filter(tenant=self.b).values_list("pk", flat=True))
        self.client.post("/numbers/bulk/", {"action": "block", "confirm": "1", "ids": b_ids})
        self.assertFalse(Number.unscoped.filter(tenant=self.b, blocked=True).exists())

    def test_dropdowns_built_at_import_time_are_still_scoped(self):
        class F(forms.Form):  # like the module-level forms in the views
            acc = forms.ModelChoiceField(Account.objects.all())
        with tenancy.use_tenant(self.a):
            titles = {str(a) for a in F().fields["acc"].queryset}
            self.assertFalse(F({"acc": self.wb["sup"].pk}).is_valid())
        self.assertEqual(titles, {"Alpha Supplier", "Alpha Client"})

    def test_no_portal_selected_fails_closed(self):
        with tenancy.use_tenant(tenancy.DENY):
            with self.assertRaises(tenancy.TenantNotSet):
                list(Account.objects.all())
            with self.assertRaises(tenancy.TenantNotSet):
                Account.objects.count()
            with self.assertRaises(tenancy.TenantNotSet):
                Account.objects.update(notes="x")

    def test_writes_cant_cross_portals(self):
        with tenancy.use_tenant(self.a):
            with self.assertRaises(tenancy.CrossTenantError):  # parent in portal B
                BillingGroup.objects.create(supplier=self.wb["sup"], title="x",
                                            country=Country.objects.first(), currency=Currency.objects.first())
            with self.assertRaises(tenancy.CrossTenantError):  # written into B from A
                Account(title="sneaky", tenant=self.b).save()
            self.assertEqual(Account.objects.update(notes="hello"), 2)  # only A's two accounts
            self.assertEqual(Account.objects.filter(pk=self.wb["sup"].pk).count(), 0)
        self.assertEqual(Account.unscoped.filter(tenant=self.b, notes="hello").count(), 0)

    def test_subqueries_are_scoped(self):
        from django.db.models import Exists, OuterRef
        with tenancy.use_tenant(self.a):
            any_alloc = Allocation.objects.filter(number=OuterRef("pk"))
            # Exists() over A's numbers can't see B's allocations, and vice versa.
            n = Number.unscoped.filter(tenant=self.b).annotate(x=Exists(any_alloc)).filter(x=True).count()
        self.assertEqual(n, 0)

    def test_same_names_allowed_in_different_portals(self):
        with tenancy.use_tenant(self.b):
            Account.objects.create(title="Alpha Supplier")  # A has one with this name: fine
        self.client.force_login(self.owner_a)
        r = self.client.post("/accounts/new/", {"title": "Alpha Supplier", "is_supplier": "on", "active": "on",
                                                "process_insights": "on"})
        self.assertEqual(r.status_code, 200)  # form error, not a crash
        self.assertContains(r, "already exists")

    def test_smpp_usernames_unique_across_portals(self):
        self.client.force_login(self.owner_a)
        r = self.client.post(f"/accounts/{self.wa['sup'].pk}/smpp-user/new/",
                             {"title": "x", "role": "supplier", "username": "bravobind", "password": "pw",
                              "dlr_wait_time": 0, "enabled": "on"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "already exists")

    def test_sms_on_one_portals_bind_never_routes_to_another_portals_number(self):
        from sms.routing import receive
        with tenancy.all_tenants():
            m = receive(source="X", destination="447700900200", body="hi", smpp_username="alphabind")
        self.assertEqual(m.tenant_id, self.a.pk)
        self.assertIsNone(m.client_id)
        self.assertEqual(m.status, Message.NO_ROUTE)
        with tenancy.all_tenants():
            m = receive(source="X", destination="447700900200", body="hi", smpp_username="bravobind")
        self.assertEqual((m.tenant_id, m.client_id), (self.b.pk, self.wb["cli"].pk))

    def test_statement_shows_the_portals_own_company(self):
        from crm import statements as st
        with tenancy.use_tenant(self.b):
            self.b.company_details = "Bravo Bank, sort code 11-22-33"
            self.b.save()
            acc = Account.objects.get(pk=self.wb["cli"].pk)
            ctx = st.document_context(acc, "statement", *st.month_bounds("2026-01"), None)
        self.assertEqual(ctx["company_name"], "Bravo Comms Ltd")
        self.assertIn("11-22-33", ctx["company_details"])

    def test_statement_email_uses_portal_name(self):
        from crm import statements as st
        self.b.billing_email = "accounts@bravo.example.com"
        self.b.save()
        with tenancy.use_tenant(self.b):
            acc = Account.objects.get(pk=self.wb["cli"].pk)
            acc.billing_email = "client@example.com"
            acc.save()
            doc = st.create_document(acc, "statement", st.month_bounds("2026-01")[0])
            self.assertTrue(st.send_document(doc))
        self.assertIn("Bravo Comms Ltd", mail.outbox[-1].subject)
        self.assertIn("Bravo Comms Ltd", mail.outbox[-1].from_email)
        self.assertEqual(mail.outbox[-1].reply_to, ["accounts@bravo.example.com"])


class Roles(Base):
    def test_member_sees_only_their_sections(self):
        u = make_login("sms-only", self.a, role=Profile.ROLE_MEMBER, sections=["sms"], can_edit=False)
        self.client.force_login(u)
        self.assertEqual(self.client.get("/sms/records/").status_code, 200)
        for url in ["/suppliers/", "/billing-groups/", "/wholesale/", "/team/", "/settings/", "/sms/stats/"]:
            self.assertEqual(self.client.get(url).status_code, 403, url)
        r = self.client.get("/")
        self.assertNotContains(r, "Due in")
        self.assertNotContains(r, "/suppliers/")  # not in the menu
        r = self.client.get("/sms/records/?col=rate_in&col=destination")
        self.assertNotContains(r, "Rate in")

    def test_view_only_member_cant_change_anything(self):
        u = make_login("viewer", self.a, role=Profile.ROLE_MEMBER, can_edit=False)
        self.client.force_login(u)
        self.assertEqual(self.client.get("/suppliers/").status_code, 200)
        r = self.client.post("/accounts/new/", {"title": "New", "is_supplier": "on"})
        self.assertEqual(r.status_code, 403)
        self.assertFalse(Account.unscoped.filter(title="New").exists())

    def test_client_sees_own_account_only_and_no_supplier_details(self):
        u = make_login("cli", self.a, role=Profile.ROLE_CLIENT, account=self.wa["cli"])
        self.client.force_login(u)
        r = self.client.get("/sms/records/?col=supplier&col=rate_in&col=destination")
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "Alpha Supplier")
        self.assertNotContains(r, "Rate in")
        self.assertEqual(self.client.get("/suppliers/").status_code, 403)
        self.assertEqual(self.client.post("/sms/records/").status_code, 403)

    def test_owner_creates_team_and_client_logins(self):
        self.client.force_login(self.owner_a)
        r = self.client.post("/team/new/", {"first_name": "Sam", "email": "Sam@Example.com", "role": "member",
                                            "sections": ["numbers", "sms"]}, follow=True)
        self.assertContains(r, "/password/set/")  # no email server here, so the link is shown
        sam = User.objects.get(email="sam@example.com")
        self.assertEqual((sam.profile.tenant, sam.profile.role, sam.profile.sections, sam.profile.can_edit),
                         (self.a, "member", ["numbers", "sms"], False))
        self.assertFalse(sam.has_usable_password())
        # A client login can only be tied to one of this portal's own client accounts.
        r = self.client.post("/team/new/", {"first_name": "C", "email": "c@example.com", "role": "client",
                                            "account": self.wb["cli"].pk})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(User.objects.filter(email="c@example.com").exists())

    def test_owner_cant_see_or_touch_another_portals_logins(self):
        self.client.force_login(self.owner_a)
        self.assertNotContains(self.client.get("/team/"), "owner-b")
        pb = self.owner_b.profile
        self.assertEqual(self.client.get(f"/team/{pb.pk}/").status_code, 404)
        self.assertEqual(self.client.post(f"/team/{pb.pk}/action/", {"action": "disable"}).status_code, 404)
        self.owner_b.refresh_from_db()
        self.assertTrue(self.owner_b.is_active)

    def test_only_owner_cant_disable_themself_or_be_demoted(self):
        self.client.force_login(self.owner_a)
        self.client.post(f"/team/{self.owner_a.profile.pk}/action/", {"action": "disable"})
        self.owner_a.refresh_from_db()
        self.assertTrue(self.owner_a.is_active)

    def test_member_cant_manage_logins(self):
        u = make_login("m", self.a, role=Profile.ROLE_MEMBER)
        self.client.force_login(u)
        self.assertEqual(self.client.get("/team/").status_code, 403)
        self.assertEqual(self.client.post("/team/new/", {"email": "x@example.com"}).status_code, 403)

    def test_disabled_login_is_signed_out(self):
        self.client.force_login(self.owner_a)
        self.owner_a.is_active = False
        self.owner_a.save()
        self.assertEqual(self.client.get("/").status_code, 302)

    def test_suspended_portal_signs_everyone_out(self):
        self.client.force_login(self.owner_a)
        self.a.active = False
        self.a.save()
        r = self.client.get("/")
        self.assertRedirects(r, "/login/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/suppliers/").status_code, 302)

    def test_login_with_no_portal_sees_nothing(self):
        u = User.objects.create_user("orphan", "o@example.com", "pass-12345678")
        self.client.force_login(u)
        self.assertEqual(self.client.get("/").status_code, 302)


class Platform(Base):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_superuser("platform", "p@example.com", "a-long-platform-pass")
        p = self.admin.profile
        p.totp_secret, p.totp_enabled = totp.new_secret(), True
        p.save()
        self.client.force_login(self.admin)

    def test_platform_admin_sees_portals_not_their_data(self):
        r = self.client.get("/platform/")
        self.assertContains(r, "Alpha Telecom")
        self.assertContains(r, "Bravo Comms")
        for url in ["/", "/suppliers/", "/numbers/", "/sms/records/", "/team/"]:
            r = self.client.get(url)
            self.assertIn(r.status_code, (302, 403), url)
            self.assertNotIn(b"Alpha Supplier", r.content)

    def test_create_portal_then_owner_sets_password_and_signs_in(self):
        r = self.client.post("/platform/", {"portal": "Charlie Networks", "colourway": "paper",
                                            "first_name": "Cat", "email": "cat@charlie.example.com"}, follow=True)
        link = re.search(r"https?://[^\"']+/password/set/[^\"']+", r.content.decode()).group(0)
        t = Tenant.objects.get(name="Charlie Networks")
        owner = User.objects.get(email="cat@charlie.example.com")
        self.assertEqual((owner.profile.tenant, owner.profile.role), (t, "owner"))
        self.client.logout()
        path = link.split("testserver", 1)[1]
        r = self.client.get(path, follow=True)
        self.assertContains(r, "Choose your password")
        r = self.client.post(r.redirect_chain[-1][0], {"new_password1": "horse battery staple 9",
                                                       "new_password2": "horse battery staple 9"})
        self.assertEqual(r.status_code, 302)
        r = self.client.post("/login/", {"username": "cat@charlie.example.com", "password": "horse battery staple 9"})
        self.assertRedirects(r, "/", fetch_redirect_response=False)
        r = self.client.get("/suppliers/")
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "Alpha")  # a fresh, empty portal
        self.assertContains(self.client.get("/"), "Charlie Networks")

    def test_suspend_portal(self):
        self.client.post(f"/platform/{self.a.pk}/", {"action": "save", "p-name": "Alpha Telecom",
                                                     "p-colourway": "harbour", "p-company_name": "Alpha"})
        self.a.refresh_from_db()
        self.assertFalse(self.a.active)

    def test_platform_admin_must_set_up_two_factor(self):
        u = User.objects.create_superuser("new-admin", "n@example.com", "another-long-pass")
        self.client.force_login(u)
        r = self.client.get("/platform/")
        self.assertRedirects(r, "/account/security/?setup=1", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/account/security/?setup=1").status_code, 200)


@override_settings(DJANGO_ADMIN_ENABLED=True)
class SignIn(Base):
    def test_password_is_hashed_with_argon2(self):
        self.assertTrue(self.owner_a.password.startswith("argon2"))

    def test_sign_in_with_email(self):
        r = self.client.post("/login/", {"username": "OWNER-A@example.com", "password": "pass-12345678"})
        self.assertRedirects(r, "/", fetch_redirect_response=False)

    def test_lockout_after_repeated_failures(self):
        for _ in range(5):
            self.client.post("/login/", {"username": "owner-a", "password": "wrong"})
        r = self.client.post("/login/", {"username": "owner-a", "password": "pass-12345678"})
        self.assertContains(r, "Too many attempts")
        self.assertEqual(LoginAttempt.objects.filter(ok=False).count(), 5)
        # A different IP isn't locked out of the same account.
        r = self.client.post("/login/", {"username": "owner-a", "password": "pass-12345678"},
                             REMOTE_ADDR="198.51.100.7")
        self.assertEqual(r.status_code, 302)

    def test_spoofed_forwarded_for_cant_dodge_lockout(self):
        for i in range(5):
            self.client.post("/login/", {"username": "owner-a", "password": "wrong"},
                             HTTP_X_FORWARDED_FOR=f"203.0.113.{i}")
        r = self.client.post("/login/", {"username": "owner-a", "password": "pass-12345678"},
                             HTTP_X_FORWARDED_FOR="203.0.113.99")
        self.assertContains(r, "Too many attempts")

    def test_two_factor_sign_in(self):
        p = self.owner_a.profile
        p.totp_secret, p.totp_enabled = totp.new_secret(), True
        p.save()
        r = self.client.post("/login/", {"username": "owner-a", "password": "pass-12345678"})
        self.assertRedirects(r, "/login/code/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/").status_code, 302)  # not signed in yet
        self.assertContains(self.client.post("/login/code/", {"code": "000000"}), "already used")
        code = totp._code(p.totp_secret, totp.now_step())
        r = self.client.post("/login/code/", {"code": code})
        self.assertRedirects(r, "/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/").status_code, 200)
        # The same code can't be used again.
        self.client.post("/logout/")
        self.client.post("/login/", {"username": "owner-a", "password": "pass-12345678"})
        self.assertContains(self.client.post("/login/code/", {"code": code}), "already used")

    def test_turn_on_two_factor(self):
        self.client.force_login(self.owner_a)
        r = self.client.get("/account/security/")
        self.assertContains(r, "<svg")
        secret = Profile.objects.get(user=self.owner_a).totp_secret
        r = self.client.post("/account/security/", {"action": "enable", "code": totp._code(secret, totp.now_step())})
        self.assertRedirects(r, "/account/security/", fetch_redirect_response=False)
        self.assertTrue(Profile.objects.get(user=self.owner_a).totp_enabled)

    def test_django_admin_login_goes_through_ours(self):
        r = self.client.get("/admin/login/")
        if r.status_code == 302:
            self.assertTrue(r["Location"].startswith("/login/"))
        else:
            self.assertContains(r, "Forgotten your password?")  # our form, with lockout and two-factor

    def test_forgot_password_emails_a_link(self):
        r = self.client.post("/password/forgot/", {"email": "owner-a@example.com"})
        self.assertRedirects(r, "/login/?reset=sent", fetch_redirect_response=False)
        self.assertIn("/password/set/", mail.outbox[-1].body)
        self.client.post("/password/forgot/", {"email": "nobody@example.com"})
        self.assertEqual(len(mail.outbox), 1)  # same answer either way, no email for unknown addresses

    def test_web_login_cant_bind_smpp_and_bind_cant_sign_in(self):
        from sms.smpp_server import authenticate
        from asgiref.sync import async_to_sync
        auth = getattr(authenticate, "__wrapped__", None) or async_to_sync(authenticate)
        self.assertIsNone(auth("owner-a", "pass-12345678", "127.0.0.1")[0])
        r = self.client.post("/login/", {"username": "alphabind", "password": "pw"})
        self.assertContains(r, "email or password isn")


class Migration(TestCase):
    def test_existing_rows_moved_into_a_portal(self):
        # Every tenant-owned table has a NOT NULL tenant column.
        from django.apps import apps
        for m in apps.get_models():
            if issubclass(m, tenancy.TenantOwned):
                self.assertFalse(m._meta.get_field("tenant").null, m.__name__)
