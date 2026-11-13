"""Fixes from the customer walkthrough: number upload mistakes, the simpler billing group form,
bad query parameters, the currency-aware dashboard and the client portal statements."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from core.models import Country, Currency, PaymentTerms, Profile, TimeOfDay
from crm.models import Account
from inventory import services
from inventory.models import BillingGroup, BillingGroupRate, Number
from sms.models import Message


class Base(TestCase):
    def setUp(self):
        call_command("seed_lookups", verbosity=0)
        self.admin = User.objects.create_superuser("admin", "a@example.com", "pass-12345678")
        self.client.force_login(self.admin)
        self.terms = PaymentTerms.objects.get(title="30/45")
        self.uk = Country.objects.get(name="United Kingdom")
        self.gbp = Currency.objects.get(code="GBP")
        self.supplier = Account.objects.create(title="Sup", is_supplier=True, currency=self.gbp)
        self.cust = Account.objects.create(title="Cust", is_client=True)
        self.group = BillingGroup.objects.create(supplier=self.supplier, title="UK A", country=self.uk,
                                                 currency=self.gbp)
        BillingGroupRate.objects.create(billing_group=self.group, slot=1, payment_terms=self.terms,
                                        time_of_day=TimeOfDay.objects.get(title="All Times"),
                                        rate_in=Decimal("0.02"), rate_out=Decimal("0.015"))


class NumberUploadTests(Base):
    def post(self, numbers, **extra):
        return self.client.post("/numbers/add/", {"billing_group": self.group.pk, "mode": "list",
                                                  "numbers": numbers, **extra})

    def test_local_format_gets_country_code(self):
        self.post("07700 900123")
        self.assertTrue(Number.objects.filter(number="447700900123").exists())

    def test_excel_scientific_notation_is_refused(self):
        r = self.post("4.47701E+11")
        self.assertContains(r, "E+")
        self.assertFalse(Number.objects.exists())

    def test_foreign_numbers_need_a_tick(self):
        r = self.post("33612345678")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Number.objects.exists())
        self.post("33612345678", other_country="on")
        self.assertTrue(Number.objects.filter(number="33612345678").exists())

    def test_bad_query_parameters_do_not_crash(self):
        for url in ("/numbers/?per=abc", "/numbers/?per=999999", "/sms/stats/?by=zzz", "/voice/stats/?by=zzz"):
            self.assertEqual(self.client.get(url).status_code, 200, url)


class BillingGroupFormTests(Base):
    def form_data(self, url, **overrides):
        r = self.client.get(url)
        rates = r.context["rates"]
        data = {k: v for k, v in r.context["form"].initial.items() if v is not None}
        data.update({"supplier": self.supplier.pk, "title": "New range", "country": self.uk.pk,
                     "currency": self.gbp.pk, "routing_type": "sms", "active": "on",
                     "billing_increment_first": 60, "billing_increment_next": 60, "min_duration": 0})
        for k, v in rates.management_form.initial.items():
            data[f"{rates.prefix}-{k}"] = v
        for f in rates.forms:
            for name, field in f.fields.items():
                v = f[name].value()
                if v not in (None, "", False):
                    data[f"{f.prefix}-{name}"] = getattr(v, "pk", v)
        data.update(overrides)
        return rates, data

    def test_simple_form_has_one_rate_row_per_channel(self):
        rates, _ = self.form_data("/billing-groups/new/")
        simple = [f["channel"].value() for f in rates.forms if f.is_simple]
        self.assertEqual(sorted(simple), ["sms", "voice"])

    def test_save_simple_sms_rate(self):
        rates, data = self.form_data("/billing-groups/new/")
        sms = next(f for f in rates.forms if f.is_simple and f["channel"].value() == "sms")
        data.update({f"{sms.prefix}-rate_in": "0.03", f"{sms.prefix}-rate_out": "0.02"})
        r = self.client.post("/billing-groups/new/", data)
        self.assertEqual(r.status_code, 302, getattr(r, "context", None) and r.context["rates"].errors)
        g = BillingGroup.objects.get(title="New range")
        self.assertEqual(list(g.rates.values_list("channel", "rate_in")), [("sms", Decimal("0.03000"))])

    def test_existing_all_times_rate_stays_simple(self):
        rates, _ = self.form_data(f"/billing-groups/{self.group.pk}/edit/")
        saved = [f for f in rates.forms if f.instance.pk]
        self.assertTrue(saved[0].is_simple)
        # no second empty slot-1 SMS row next to the saved one
        self.assertEqual(sum(1 for f in rates.forms if f["channel"].value() == "sms" and str(f["slot"].value()) == "1"), 1)

    def test_duplicate_rates_are_rejected(self):
        rates, data = self.form_data("/billing-groups/new/")
        sms_rows = [f for f in rates.forms if f["channel"].value() == "sms"][:2]
        for f in sms_rows:
            data.update({f"{f.prefix}-slot": 1, f"{f.prefix}-rate_in": "0.03", f"{f.prefix}-rate_out": "0.02"})
        r = self.client.post("/billing-groups/new/", data)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(BillingGroup.objects.filter(title="New range").exists())


class DashboardTests(Base):
    def test_currencies_are_not_added_together(self):
        eur = Currency.objects.get(code="EUR")
        now = timezone.now() - timedelta(hours=1)
        for cur, amount in (("GBP", "5"), ("EUR", "3")):
            Message.objects.create(received_at=now, source="x", destination="1", body="b", status="forwarded",
                                   client=self.cust, supplier=self.supplier, currency=cur,
                                   rate_in=Decimal(amount), rate_out=Decimal("1"))
        Message.objects.create(received_at=now, source="x", destination="2", body="b", status="no_route")
        r = self.client.get("/?period=today")
        self.assertEqual(r.context["cur"], "GBP")
        self.assertEqual(r.context["t"]["due_in"], Decimal("5"))
        self.assertEqual(r.context["other_currencies"][0][0], "EUR")
        self.assertEqual(r.context["unrouted"]["sms"], 1)
        self.assertEqual(r.context["dl"]["sms_pct"], 100)
        self.assertNotIn(eur.code, [r.context["cur"]])


class ClientPortalTests(Base):
    def setUp(self):
        super().setUp()
        self.other = Account.objects.create(title="Other", is_client=True)
        u = User.objects.create_user("c", password="pass-12345678")
        Profile.objects.update_or_create(user=u, defaults={"role": Profile.ROLE_CLIENT, "account": self.cust})
        self.client.force_login(u)

    def test_client_sees_own_statements_only(self):
        month = (timezone.localdate().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        self.assertEqual(self.client.get("/statements/").status_code, 200)
        self.assertEqual(self.client.get(f"/accounts/{self.cust.pk}/documents/statement/{month}/").status_code, 200)
        self.assertEqual(self.client.get(f"/accounts/{self.other.pk}/documents/statement/{month}/").status_code, 403)
        self.assertEqual(self.client.get(f"/accounts/{self.cust.pk}/documents/invoice/{month}/").status_code, 403)

    def test_client_cannot_break_down_by_billing_group(self):
        services.add_numbers(self.group, ["447700900100"])
        r = self.client.get("/statements/?by=group")
        self.assertEqual(r.context["by"], "day")
        self.assertNotIn("group", dict(r.context["breakdowns"]))

    def test_client_dashboard_has_account_panel(self):
        r = self.client.get("/")
        self.assertContains(r, "Your account")
        self.assertNotContains(r, "Unrouted")


class BlockRuleScopeTests(Base):
    def setUp(self):
        super().setUp()
        from crm.models import HttpEndpoint
        HttpEndpoint.objects.create(account=self.cust, title="hook", url="https://example.com/hook")
        services.add_numbers(self.group, ["447700900100", "447700900101"])
        services.allocate(list(Number.objects.all()), self.cust, self.terms)
        self.other_sup = Account.objects.create(title="Other sup", is_supplier=True)
        self.other_group = BillingGroup.objects.create(supplier=self.other_sup, title="UK B", country=self.uk,
                                                       currency=self.gbp)

    def receive(self, to="447700900100", sender="ShopCo"):
        from sms.routing import receive
        return receive(source=sender, destination=to, body="hi")

    def test_supplier_rule_applies_to_that_suppliers_traffic(self):
        from sms.models import BlockRule
        BlockRule.objects.create(scope=BlockRule.SUPPLIER, account=self.supplier, rule_type="sender", value="shopco")
        m = self.receive()
        self.assertEqual(m.status, Message.BLOCKED)
        self.assertIsNotNone(m.block_rule)
        BlockRule.objects.update(account=self.other_sup)
        self.assertNotEqual(self.receive().status, Message.BLOCKED)

    def test_group_and_client_rules(self):
        from sms.models import BlockRule
        r = BlockRule.objects.create(scope=BlockRule.GROUP, billing_group=self.other_group, rule_type="prefix",
                                     value="4477")
        self.assertNotEqual(self.receive().status, Message.BLOCKED)
        r.billing_group = self.group
        r.save()
        self.assertEqual(self.receive().status, Message.BLOCKED)
        r.delete()
        BlockRule.objects.create(scope=BlockRule.CLIENT, account=self.cust, rule_type="number", value="447700900101")
        self.assertEqual(self.receive("447700900101").status, Message.BLOCKED)
        self.assertNotEqual(self.receive("447700900100").status, Message.BLOCKED)

    def test_limit_per_number_per_hour(self):
        from sms.models import BlockRule
        BlockRule.objects.create(scope=BlockRule.ALL, rule_type="limit", value="2")
        statuses = [self.receive().status for _ in range(3)]
        self.assertNotEqual(statuses[1], Message.BLOCKED)
        self.assertEqual(statuses[2], Message.BLOCKED)
        self.assertNotEqual(self.receive("447700900101").status, Message.BLOCKED)  # other numbers unaffected

    def test_rules_from_allocate_step(self):
        from sms.models import BlockRule
        ids = list(Number.objects.values_list("pk", flat=True))
        self.client.post("/numbers/bulk/", {"action": "allocate", "confirm": "1", "ids": ids, "client": self.cust.pk,
                                            "payment_terms": self.terms.pk, "use_for": "both",
                                            "rules-limit": "15", "rules-senders": "SpamCo, Junk"})
        rules = BlockRule.objects.filter(scope=BlockRule.CLIENT, account=self.cust)
        self.assertEqual(sorted(rules.values_list("rule_type", "value")),
                         [("limit", "15"), ("sender", "Junk"), ("sender", "SpamCo")])
        # allocating again with the limit left blank keeps it
        self.client.post("/numbers/bulk/", {"action": "allocate", "confirm": "1", "ids": ids, "client": self.cust.pk,
                                            "payment_terms": self.terms.pk, "use_for": "both", "rules-senders": "Junk"})
        self.assertTrue(rules.filter(rule_type="limit", value="15").exists())
        self.assertEqual(rules.count(), 3)

    def test_blocking_page_needs_a_target(self):
        from sms.models import BlockRule
        r = self.client.post("/sms/blocking/", {"scope": "supplier", "rule_type": "sender", "value": "X"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(BlockRule.objects.exists())
        self.client.post("/sms/blocking/", {"scope": "group", "billing_group": self.group.pk, "rule_type": "limit",
                                            "value": "10"})
        self.assertTrue(BlockRule.objects.filter(scope="group", billing_group=self.group, account=None).exists())
        self.assertEqual(self.client.get("/sms/blocking/?scope=group").status_code, 200)


class BillingGroupRulesTests(BillingGroupFormTests):
    def test_group_form_saves_and_removes_rules(self):
        from sms.models import BlockRule
        url = f"/billing-groups/{self.group.pk}/edit/"
        _, data = self.form_data(url, **{"rules-limit": "20", "rules-prefixes": "+44 7700 900"})
        data["title"] = "UK A"
        self.assertEqual(self.client.post(url, data).status_code, 302)
        rules = BlockRule.objects.filter(scope="group", billing_group=self.group)
        self.assertEqual(sorted(rules.values_list("rule_type", "value")), [("limit", "20"), ("prefix", "447700900")])
        prefix = rules.get(rule_type="prefix")
        _, data = self.form_data(url, remove_rule=[prefix.pk], **{"rules-limit": "20"})
        data["title"] = "UK A"
        self.client.post(url, data)
        self.assertEqual(list(rules.values_list("rule_type", "value")), [("limit", "20")])
