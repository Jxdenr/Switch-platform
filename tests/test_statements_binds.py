"""Statements and invoices, the supplier HTTP bind, and the built-in SMPP server."""
import asyncio
import threading
import time as _time
from datetime import timedelta
from decimal import Decimal

import smpplib.client
import smpplib.consts
import smpplib.gsm
from django.contrib.auth.models import User
from django.core import mail
from django.core.management import call_command
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from core.models import Country, Currency, PaymentTerms
from crm import statements as st
from crm.models import Account, Document, HttpEndpoint, HttpUser, SmppUser
from inventory import services
from inventory.models import BillingGroup, BillingGroupRate, Number
from sms.models import Message
from sms.routing import receive
from voice.billing import record_call


def setup_world(t):
    call_command("seed_lookups", verbosity=0)
    t.terms = PaymentTerms.objects.get(title="30/45")
    t.sup = Account.objects.create(title="Sup", is_supplier=True, billing_email="ap@sup.example",
                                   auto_statements=True)
    t.cli = Account.objects.create(title="Cli", is_client=True, billing_email="a@cli.example, b@cli.example",
                                   auto_statements=True)
    uk, gbp = Country.objects.get(name="United Kingdom"), Currency.objects.get(code="GBP")
    g = BillingGroup.objects.create(supplier=t.sup, title="UK", country=uk, currency=gbp, routing_type="both",
                                    billing_increment_first=60, billing_increment_next=60)
    for ch, rin, rout in [("sms", "0.02", "0.015"), ("voice", "0.06", "0.045")]:
        BillingGroupRate.objects.create(billing_group=g, channel=ch, slot=1, payment_terms=t.terms,
                                        rate_in=Decimal(rin), rate_out=Decimal(rout))
    services.add_numbers(g, ["447700900100", "447700900101"])
    services.allocate(Number.objects.all(), t.cli, t.terms)


@override_settings(COMPANY_NAME="Test Co")
class StatementTests(TestCase):
    def setUp(self):
        setup_world(self)
        HttpEndpoint.objects.create(account=self.cli, title="hook", url="https://example.com/hook")
        first, _ = st.month_bounds(None)  # last month
        self.first = first
        when = timezone.make_aware(timezone.datetime.combine(first + timedelta(days=3), timezone.datetime.min.time()))
        for _ in range(10):
            receive(source="Bank", destination="447700900100", body="code 1", received_at=when)
        record_call(uuid="c1", number_called="447700900101", billsec=61, start=when)
        receive(source="Bank", destination="447700900100", body="this month", received_at=timezone.now())

    def test_volumes_both_sides(self):
        start, end = st.aware_range(*st.month_bounds(self.first))
        sup = st.totals(st.volumes(self.sup, "supplier", start, end))
        cli = st.totals(st.volumes(self.cli, "client", start, end))
        self.assertEqual((sup["sms"], sup["calls"]), (10, 1))
        self.assertEqual(sup["total"], Decimal("0.20") + Decimal("0.12"))
        self.assertEqual(cli["total"], Decimal("0.15") + Decimal("0.09"))
        by_number = st.volumes(self.sup, "supplier", start, end, by="number")
        self.assertEqual({r["key"] for r in by_number}, {"447700900100", "447700900101"})
        sms_only = st.totals(st.volumes(self.sup, "supplier", start, end, channel="sms"))
        self.assertEqual(sms_only["calls"], 0)

    def test_monthly_send_creates_invoice_and_statement_once(self):
        call_command("send_statements", verbosity=0)
        self.assertEqual(len(mail.outbox), 2)
        inv = Document.objects.get(kind="invoice")
        self.assertEqual((inv.account, inv.sms_count, inv.total), (self.sup, 10, Decimal("0.32")))
        stm = Document.objects.get(kind="statement")
        self.assertEqual(stm.sent_to, "a@cli.example, b@cli.example")
        self.assertTrue(any(m.subject.startswith("Invoice INV-") for m in mail.outbox))
        self.assertEqual(mail.outbox[0].attachments[0][2], "text/csv")
        call_command("send_statements", verbosity=0)  # running again never double-sends
        self.assertEqual(len(mail.outbox), 2)

    def test_statement_tab_export_and_document_page(self):
        admin = User.objects.create_superuser("admin", "a@example.com", "pass-12345678")
        self.client.force_login(admin)
        r = self.client.get(f"/accounts/{self.sup.pk}/?tab=statement&by=day")
        self.assertContains(r, "Monthly invoices")
        r = self.client.get(f"/accounts/{self.sup.pk}/?tab=statement&export=csv")
        self.assertEqual(r["Content-Type"], "text/csv")
        r = self.client.get(f"/accounts/{self.cli.pk}/documents/statement/{self.first:%Y-%m}/")
        self.assertContains(r, "Amount payable to Cli")
        self.assertEqual(self.client.get("/suppliers/").status_code, 200)
        self.assertNotContains(self.client.get("/suppliers/"), ">Cli<")
        self.assertContains(self.client.get("/clients/"), ">Cli<")


class HttpBindTests(TestCase):
    def setUp(self):
        setup_world(self)
        HttpEndpoint.objects.create(account=self.cli, title="hook", url="https://example.com/hook")
        self.hu = HttpUser.objects.create(account=self.sup, title="HTTP in", ip="10.0.0.1")

    def test_supplier_sends_over_http(self):
        r = self.client.post("/api/sms/inbound/", {"key": "wrong", "to": "447700900100", "message": "x"})
        self.assertEqual(r.status_code, 403)
        r = self.client.post("/api/sms/inbound/", {"to": "+447700900100", "from": "Bank", "text": "code 1"},
                             HTTP_X_API_KEY=self.hu.api_key, REMOTE_ADDR="10.0.0.2")
        self.assertEqual(r.json()["error"], "IP not allowed")
        r = self.client.get("/api/sms/inbound/", {"key": self.hu.api_key, "msisdn": "447700900100",
                                                  "sender": "Bank", "message": "code 1"}, REMOTE_ADDR="10.0.0.1")
        self.assertTrue(r.json()["routed"])
        m = Message.objects.get()
        self.assertEqual((m.supplier, m.client, m.http_user, m.received_via), (self.sup, self.cli, self.hu, "HTTP"))


@override_settings(FORWARD_MAX_ATTEMPTS=5)
class SmppServerTests(TransactionTestCase):
    """A real SMPP session over TCP, using the smpplib client as supplier and as client."""

    def setUp(self):
        setup_world(self)
        SmppUser.objects.create(account=self.sup, title="sup bind", username="supin", password="pw1")
        self.cb = SmppUser.objects.create(account=self.cli, title="cli bind", username="cliout", password="pw2",
                                          role=SmppUser.CLIENT)
        from sms.smpp_server import SmppServer
        self.loop = asyncio.new_event_loop()
        self.server = SmppServer("127.0.0.1", 0, poll=0.1)
        self.loop.run_until_complete(self.server.start())
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        asyncio.run_coroutine_threadsafe(self.server.stop(), self.loop).result(2)
        _time.sleep(0.2)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(2)

    def _client(self):
        c = smpplib.client.Client("127.0.0.1", self.server.port, timeout=5, allow_unknown_opt_params=True)
        c.connect()
        return c

    def test_wrong_password_refused(self):
        c = self._client()
        with self.assertRaises(smpplib.exceptions.PDUError):
            c.bind_transceiver(system_id="supin", password="nope")
        c.disconnect()

    def test_supplier_submit_is_routed_and_client_receives_it(self):
        received = []
        cli = self._client()
        cli.set_message_received_handler(lambda pdu: received.append(pdu))
        cli.bind_receiver(system_id="cliout", password="pw2")
        self.cb.refresh_from_db()
        self.assertTrue(self.cb.bound)

        sup = self._client()
        sup.bind_transceiver(system_id="supin", password="pw1")
        resp = []
        sup.set_message_sent_handler(lambda pdu: resp.append(pdu))
        sup.send_message(source_addr_ton=5, source_addr="MyBank", dest_addr_ton=1, destination_addr="447700900100",
                         short_message=b"Your code is 123456", data_coding=0)
        sup.read_once()  # submit_sm_resp
        self.assertEqual(resp[0].status, 0)
        m = Message.objects.get()
        self.assertEqual((m.supplier, m.client, m.client_bind, m.body), (self.sup, self.cli, self.cb,
                                                                          "Your code is 123456"))
        cli.read_once()  # deliver_sm; smpplib answers deliver_sm_resp itself
        self.assertEqual(received[0].short_message, b"Your code is 123456")
        self.assertEqual(received[0].destination_addr, b"447700900100")
        for _ in range(30):
            m.refresh_from_db()
            if m.status == Message.FORWARDED:
                break
            _time.sleep(0.1)
        self.assertEqual(m.status, Message.FORWARDED)
        sup.unbind(); cli.unbind()
        sup.disconnect(); cli.disconnect()

    def test_unicode_and_client_cannot_submit(self):
        received = []
        cli = self._client()
        cli.set_message_received_handler(lambda pdu: received.append(pdu))
        cli.bind_transceiver(system_id="cliout", password="pw2")
        receive(source="Shop", destination="447700900101", body="Code: 42 ✓")
        cli.read_once()
        self.assertEqual(received[0].data_coding, 8)
        self.assertEqual(received[0].short_message.decode("utf-16-be"), "Code: 42 ✓")
        sent = []
        cli.set_message_sent_handler(lambda pdu: sent.append(pdu))
        cli.send_message(source_addr="1", destination_addr="447700900100", short_message=b"x")
        try:
            cli.read_once()
        except smpplib.exceptions.PDUError:
            pass
        self.assertEqual(Message.objects.count(), 1)
        cli.disconnect()
