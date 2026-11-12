"""End-to-end checks of the core flows: add numbers, allocate, route an inbound SMS,
forward it, unallocate, and client-portal isolation."""
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase, override_settings

from core.models import Country, Currency, PaymentTerms, TimeOfDay
from crm.models import Account, HttpEndpoint, SmppUser
from inventory import services
from inventory.models import Allocation, BillingGroup, BillingGroupRate, Number
from sms.models import BlockRule, Message


@override_settings(INGEST_TOKEN="t0k")
class FlowTests(TestCase):
    def setUp(self):
        call_command("seed_lookups", verbosity=0)
        self.admin = User.objects.create_superuser("admin", "a@example.com", "pass-12345678")
        self.client.force_login(self.admin)
        self.terms = PaymentTerms.objects.get(title="30/45")
        self.supplier = Account.objects.create(title="Sup", is_supplier=True)
        self.cust = Account.objects.create(title="Cust", is_client=True)
        HttpEndpoint.objects.create(account=self.cust, title="hook", url="https://example.com/hook")
        SmppUser.objects.create(account=self.supplier, title="b", username="supbind", password="x")
        self.group = BillingGroup.objects.create(supplier=self.supplier, title="UK A",
                                                 country=Country.objects.get(name="United Kingdom"),
                                                 currency=Currency.objects.get(code="GBP"))
        BillingGroupRate.objects.create(billing_group=self.group, slot=1, payment_terms=self.terms,
                                        time_of_day=TimeOfDay.objects.get(title="All Times"),
                                        rate_in=Decimal("0.02"), rate_out=Decimal("0.015"))

    def test_add_numbers_range_list_and_duplicates(self):
        r = self.client.post("/numbers/add/", {"billing_group": self.group.pk, "mode": "range",
                                                "start": "447700900100", "quantity": 5})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Number.objects.count(), 5)
        self.client.post("/numbers/add/", {"billing_group": self.group.pk, "mode": "list",
                                           "numbers": "+44 7700 900104\n447700900200, 447700900201"})
        self.assertEqual(Number.objects.count(), 7)  # 900104 was a duplicate

    def test_allocate_route_forward_unallocate(self):
        services.add_numbers(self.group, ["447700900100", "447700900101"])
        ids = list(Number.objects.values_list("pk", flat=True))
        r = self.client.post("/numbers/bulk/", {"action": "allocate", "ids": ids})
        self.assertContains(r, "Allocate 2 numbers")  # confirm step, nothing changed yet
        self.assertFalse(Allocation.objects.exists())
        self.client.post("/numbers/bulk/", {"action": "allocate", "confirm": "1", "ids": ids,
                                            "client": self.cust.pk, "payment_terms": self.terms.pk,
                                            "use_for": "both"})
        self.assertEqual(Allocation.objects.filter(ends_at__isnull=True).count(), 2)

        r = self.client.post("/ingest/jasmin/?token=t0k&smpp_user=supbind",
                             {"from": "ExampleCo", "to": "+447700900100", "content": "hello"})
        self.assertEqual(r.content, b"ACK/Jasmin")
        m = Message.objects.get()
        self.assertEqual((m.client, m.supplier, m.status), (self.cust, self.supplier, Message.RECEIVED))
        self.assertEqual((m.rate_in, m.rate_out), (Decimal("0.02"), Decimal("0.015")))

        with mock.patch("sms.routing.requests.post") as post:
            post.return_value.status_code = 200
            call_command("forward_pending", verbosity=0)
            post.assert_called_once()
            self.assertEqual(post.call_args.kwargs["data"]["message"], "hello")
        m.refresh_from_db()
        self.assertEqual(m.status, Message.FORWARDED)

        self.client.post("/numbers/bulk/", {"action": "unallocate", "confirm": "1", "ids": ids})
        self.assertFalse(Allocation.objects.filter(ends_at__isnull=True).exists())
        self.assertEqual(Allocation.objects.count(), 2)  # history kept
        self.client.post("/ingest/jasmin/?token=t0k", {"from": "X", "to": "447700900100", "content": "x"})
        self.assertEqual(Message.objects.latest("pk").status, Message.NO_ROUTE)

    def test_ingest_rejects_bad_token(self):
        r = self.client.post("/ingest/jasmin/?token=nope", {"from": "a", "to": "1", "content": "x"})
        self.assertEqual(r.status_code, 403)

    def test_block_rule(self):
        services.add_numbers(self.group, ["447700900100"])
        services.allocate(Number.objects.all(), self.cust, self.terms)
        BlockRule.objects.create(rule_type=BlockRule.SENDER, value="spammer")
        self.client.post("/ingest/jasmin/?token=t0k", {"from": "Spammer", "to": "447700900100", "content": "x"})
        m = Message.objects.get()
        self.assertEqual((m.status, m.rate_out), (Message.BLOCKED, 0))

    def test_select_all_matching(self):
        services.add_numbers(self.group, [f"4477009001{i:02d}" for i in range(60)])
        self.client.post("/numbers/bulk/", {"action": "block", "confirm": "1", "select_all_matching": "1",
                                            "f_group": self.group.pk})
        self.assertEqual(Number.objects.filter(blocked=True).count(), 60)

    def test_client_portal_sees_only_own_data(self):
        other = Account.objects.create(title="Other", is_client=True)
        services.add_numbers(self.group, ["447700900100", "447700900101"])
        services.allocate(Number.objects.filter(number="447700900100"), self.cust, self.terms)
        services.allocate(Number.objects.filter(number="447700900101"), other, self.terms)
        u = User.objects.create_user("portal", password="pass-12345678")
        u.profile.role, u.profile.account = "client", self.cust
        u.profile.save()
        self.client.force_login(u)
        r = self.client.get("/numbers/")
        self.assertContains(r, "+447700900100")
        self.assertNotContains(r, "+447700900101")
        self.assertEqual(self.client.get("/accounts/").status_code, 403)
        self.assertEqual(self.client.post("/numbers/bulk/", {"action": "unallocate"}).status_code, 403)


class ChannelTests(TestCase):
    """Numbers in an 'SMS & voice' group are uploaded once and can be allocated per channel."""

    def setUp(self):
        call_command("seed_lookups", verbosity=0)
        self.terms = PaymentTerms.objects.get(title="30/45")
        sup = Account.objects.create(title="Sup", is_supplier=True)
        self.a = Account.objects.create(title="A", is_client=True)
        self.b = Account.objects.create(title="B", is_client=True)
        uk, gbp = Country.objects.get(name="United Kingdom"), Currency.objects.get(code="GBP")
        self.both = BillingGroup.objects.create(supplier=sup, title="Both", country=uk, currency=gbp, routing_type="both")
        self.sms_only = BillingGroup.objects.create(supplier=sup, title="SMS", country=uk, currency=gbp, routing_type="sms")
        for g in (self.both, self.sms_only):
            BillingGroupRate.objects.create(billing_group=g, channel="sms", slot=1, payment_terms=self.terms,
                                            rate_in=Decimal("0.02"), rate_out=Decimal("0.01"))
        BillingGroupRate.objects.create(billing_group=self.both, channel="voice", slot=1, payment_terms=self.terms,
                                        rate_in=Decimal("0.05"), rate_out=Decimal("0.03"))
        services.add_numbers(self.both, ["447700900100"])
        services.add_numbers(self.sms_only, ["447700900200"])

    def test_default_allocates_every_supported_channel(self):
        services.allocate(Number.objects.all(), self.a, self.terms)
        both = Number.objects.get(number="447700900100")
        self.assertEqual(sorted(both.allocations.values_list("channel", flat=True)), ["sms", "voice"])
        self.assertEqual(both.active_allocation("voice").rates.get().rate_out, Decimal("0.03"))
        sms = Number.objects.get(number="447700900200")
        self.assertEqual(list(sms.allocations.values_list("channel", flat=True)), ["sms"])

    def test_split_channels_and_partial_unallocate(self):
        n = Number.objects.filter(number="447700900100")
        services.allocate(n, self.a, self.terms, channels=["sms"])
        services.allocate(n, self.b, self.terms, channels=["voice"])
        num = n.get()
        self.assertEqual((num.active_allocation("sms").client, num.active_allocation("voice").client), (self.a, self.b))
        services.unallocate(n, channels=["voice"])
        self.assertIsNone(num.active_allocation("voice"))
        self.assertEqual(num.active_allocation("sms").client, self.a)

    def test_voice_request_skips_sms_only_numbers(self):
        created, _ = services.allocate(Number.objects.filter(number="447700900200"), self.a, self.terms,
                                       channels=["voice"])
        self.assertEqual(created, 0)
