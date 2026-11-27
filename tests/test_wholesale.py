"""Wholesale: accept/refuse, routing, billing, honest receipts, test binds, and a full SMPP round
trip (customer -> our SMPP server -> router -> fake vendor SMSC -> receipt back to customer)."""
import asyncio
import struct
import threading
import time as _time
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import smpplib.client
import smpplib.consts
from django.core.management import call_command
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from core.models import Country
from crm.models import Account, SmppUser
from sms.models import BlockRule
from sms.smpp_server import cstr, pdu
from wholesale import engine, pdu as wpdu
from wholesale.models import (ApiKey, BuyRate, Customer, LedgerEntry, Network, NetworkPrefix, OutboundMessage as M,
                              RouteEntry, RoutePlan, SellRate, SmppTestSettings, Vendor, VendorBind)


def world(t, balance="10"):
    call_command("seed_lookups", verbosity=0)
    t.uk = Country.objects.get(name="United Kingdom")
    t.ee = Network.objects.create(country=t.uk, name="EE", mcc="234", mnc="30")
    NetworkPrefix.objects.create(network=t.ee, prefix="447700")
    t.cust_acc = Account.objects.create(title="Buyer Ltd")
    t.cust = Customer.objects.create(account=t.cust_acc, balance=Decimal(balance))
    SellRate.objects.create(account=t.cust_acc, country=t.uk, rate=Decimal("0.030"))
    t.v1 = Account.objects.create(title="Cheap Vendor")
    t.v2 = Account.objects.create(title="Good Vendor")
    for v, rate in ((t.v1, "0.010"), (t.v2, "0.020")):
        Vendor.objects.create(account=v)
        BuyRate.objects.create(account=v, country=t.uk, rate=Decimal(rate))
    t.b1 = VendorBind.objects.create(account=t.v1, title="v1", host="127.0.0.1", port=1, username="me", password="pw")
    t.b2 = VendorBind.objects.create(account=t.v2, title="v2", host="127.0.0.1", port=1, username="me", password="pw")


def fields(dest="447700900123", src="MyBank", body="Your code is 1234", **kw):
    return dict(source=src, destination=dest, payload=body.encode(), body=body, registered_delivery=1, **kw)


class AcceptTests(TestCase):
    def setUp(self):
        world(self)

    def test_accepts_holds_credit_and_finds_network(self):
        r = engine.accept(self.cust_acc, fields())
        self.assertEqual(r.status, engine.ROK)
        m = r.message
        self.assertEqual((m.status, m.network, m.country, m.sell_rate), (M.QUEUED, self.ee, self.uk, Decimal("0.030")))
        self.cust.refresh_from_db()
        self.assertEqual(self.cust.balance, Decimal("9.970"))

    def test_refusals_are_recorded_with_reasons(self):
        cases = [
            (fields(dest="12"), engine.RINVDSTADR, "valid international"),
            (fields(dest="33612345678"), engine.RINVDSTADR, "No rate"),
        ]
        for f, code, why in cases:
            r = engine.accept(self.cust_acc, f)
            self.assertEqual(r.status, code)
            self.assertIn(why, r.reason)
            self.assertEqual(r.message.status, M.REFUSED)
        self.cust.allowed_senders = "MyBank, Shop"
        self.cust.save()
        self.assertEqual(engine.accept(self.cust_acc, fields(src="PayPal")).status, engine.RINVSRCADR)
        BlockRule.objects.create(rule_type=BlockRule.CONTENT, value="bit.ly", scope=BlockRule.CUSTOMER,
                                 account=self.cust_acc)
        r = engine.accept(self.cust_acc, fields(body="Win a prize bit.ly/x"))
        self.assertEqual(r.status, engine.RSUBMITFAIL)
        self.assertIn("Blocked by rule", r.reason)

    def test_prepaid_stops_at_zero_and_credit_limit_extends(self):
        self.cust.balance = Decimal("0.05")
        self.cust.save()
        self.assertEqual(engine.accept(self.cust_acc, fields()).status, engine.ROK)
        r = engine.accept(self.cust_acc, fields())
        self.assertEqual((r.status, r.reason), (engine.RX_NO_CREDIT, "Not enough credit"))
        Customer.objects.filter(pk=self.cust.pk).update(credit_limit=1)
        self.assertEqual(engine.accept(self.cust_acc, fields()).status, engine.ROK)

    def test_never_routes_at_a_loss_unless_allowed(self):
        BuyRate.objects.filter(account__in=[self.v1, self.v2]).update(rate=Decimal("0.05"))
        r = engine.accept(self.cust_acc, fields())
        self.assertIn("No route", r.reason)
        Customer.objects.filter(pk=self.cust.pk).update(allow_loss=True)
        self.assertEqual(engine.accept(self.cust_acc, fields()).status, engine.ROK)


class RoutingTests(TestCase):
    def setUp(self):
        world(self)

    def cands(self):
        self.cust.refresh_from_db()
        return [c.vendor_id for c in engine.candidates(self.cust, self.uk, self.ee, Decimal("0.03"))]

    def test_lcr_priority_and_network_rates(self):
        self.assertEqual(self.cands(), [self.v1.pk, self.v2.pk])
        BuyRate.objects.create(account=self.v2, country=self.uk, network=self.ee, rate=Decimal("0.005"))
        self.assertEqual(self.cands(), [self.v2.pk, self.v1.pk])  # network rate beats country rate
        plan = RoutePlan.objects.create(title="Premium", mode=RoutePlan.PRIORITY)
        RouteEntry.objects.create(plan=plan, country=self.uk, vendor=self.v1, priority=1)
        Customer.objects.filter(pk=self.cust.pk).update(route_plan=plan)
        self.assertEqual(self.cands(), [self.v1.pk])  # plan limits vendors to its entries

    def test_quality_floor_skips_poor_vendor(self):
        plan = RoutePlan.objects.create(title="Std", mode=RoutePlan.LCR, min_delivery_pct=90)
        Customer.objects.filter(pk=self.cust.pk).update(route_plan=plan)
        RouteEntry.objects.create(plan=plan, vendor=self.v1)
        RouteEntry.objects.create(plan=plan, vendor=self.v2)
        now = timezone.now()
        for i in range(60):
            M.objects.create(created_at=now, customer=self.cust_acc, source="x", destination="447700900000",
                             country=self.uk, vendor=self.v1, status=M.DELIVERED if i < 30 else M.UNDELIVERED)
        self.assertEqual(self.cands(), [self.v2.pk])

    def test_failover_then_honest_reject_and_refund(self):
        m = engine.accept(self.cust_acc, fields()).message
        engine.vendor_failed(m.pk, self.v1.pk, self.b1.pk, "Vendor refused 0x45")
        m.refresh_from_db()
        self.assertEqual(engine.tried_vendors(m), [self.v1.pk])
        self.assertEqual([c.vendor_id for c in engine.candidates(self.cust, self.uk, self.ee, m.sell_rate,
                                                                  engine.tried_vendors(m))], [self.v2.pk])
        with override_settings(WHOLESALE_MAX_VENDOR_ATTEMPTS=2):
            engine.vendor_failed(m.pk, self.v2.pk, self.b2.pk, "Vendor refused 0x0B")
        m.refresh_from_db()
        self.assertEqual((m.status, m.stat, m.charged, m.dlr_state), (M.REJECTED, "REJECTD", 0, M.DLR_PENDING))
        self.cust.refresh_from_db()
        self.assertEqual(self.cust.balance, Decimal("10"))


class ReceiptTests(TestCase):
    def setUp(self):
        world(self)
        self.m = engine.accept(self.cust_acc, fields()).message

    def test_hex_submit_id_matches_decimal_receipt(self):
        engine.vendor_accepted(self.m.pk, self.v1.pk, self.b1.pk, "1F4A", Decimal("0.01"))
        rec = wpdu.parse_receipt(4, b"id:8010 sub:001 dlvrd:001 submit date:2611270400 done date:2611270401 "
                                    b"stat:DELIVRD err:000 text:Your", {})
        self.assertEqual(engine.vendor_receipt(self.v1.pk, self.b1.pk, rec).pk, self.m.pk)
        self.m.refresh_from_db()
        self.assertEqual((self.m.status, self.m.stat, self.m.dlr_state), (M.DELIVERED, "DELIVRD", M.DLR_PENDING))

    def test_first_final_receipt_stands(self):
        engine.vendor_accepted(self.m.pk, self.v1.pk, self.b1.pk, "abc", Decimal("0.01"))
        for stat in ("ENROUTE", "UNDELIV", "DELIVRD"):
            engine.vendor_receipt(self.v1.pk, self.b1.pk, wpdu.parse_receipt(4, f"id:abc stat:{stat} err:001".encode(), {}))
        self.m.refresh_from_db()
        self.assertEqual(self.m.stat, "UNDELIV")

    def test_no_receipt_is_unknown_not_delivered_and_late_receipt_wins(self):
        self.cust.charge_on = Customer.DELIVERED
        self.cust.save()
        m = engine.accept(self.cust_acc, fields()).message
        engine.vendor_accepted(m.pk, self.v1.pk, self.b1.pk, "777", Decimal("0.01"))
        M.objects.filter(pk=m.pk).update(submitted_at=timezone.now() - timedelta(hours=80))
        engine.expire_unanswered()
        m.refresh_from_db()
        self.assertEqual((m.status, m.stat, m.no_receipt, m.charged), (M.UNKNOWN, "UNKNOWN", True, 0))
        self.assertIn("No receipt from route", m.reason)
        engine.vendor_receipt(self.v1.pk, self.b1.pk, wpdu.parse_receipt(4, b"id:777 stat:DELIVRD err:000", {}))
        m.refresh_from_db()
        self.assertEqual((m.status, m.charged), (M.DELIVERED, Decimal("0.030")))

    def test_tlvs_beat_text_and_receipt_round_trip(self):
        tlvs = {wpdu.TLV_RECEIPTED_ID: b"XYZ\0", wpdu.TLV_MESSAGE_STATE: bytes([5])}
        r = wpdu.parse_receipt(4, b"id:wrong stat:DELIVRD", tlvs)
        self.assertEqual((r["id"], r["stat"]), ("XYZ", "UNDELIV"))
        body = wpdu.build_receipt("MyBank", "447700900123", "ourid", "DELIVRD", "000", timezone.now(), None)
        esm, sm, tl = wpdu.parse_deliver(body)
        back = wpdu.parse_receipt(esm, sm, tl)
        self.assertEqual((esm, back["id"], back["stat"]), (4, "ourid", "DELIVRD"))


class TestBindTests(TestCase):
    def setUp(self):
        world(self)

    def test_test_bind_simulates_labelled_receipts_and_sends_nothing(self):
        key = ApiKey.objects.create(account=self.cust_acc, title="sandbox", test_mode=True, tps=1000, test_delivered_pct=50,
                                    dlr_url="https://example.com/dlr")
        stats = []
        for _ in range(40):
            r = self.client.post("/api/wholesale/send/", {"to": "447700900123", "from": "MyBank", "text": "hi"},
                                 HTTP_X_API_KEY=key.key)
            self.assertEqual(r.status_code, 200, r.content)
        for m in M.objects.all():
            self.assertTrue(m.test)
            self.assertEqual((m.err, m.vendor_id, m.sell_rate), ("TST", None, 0))
            stats.append(m.stat)
        self.assertTrue({"DELIVRD", "UNDELIV"} <= set(stats))
        with mock.patch("requests.post") as post:
            post.return_value.status_code = 200
            engine.push_http_dlrs()
        self.assertEqual(post.call_args.kwargs["data"]["test"], "1")

    def test_http_api_long_message_and_errors(self):
        key = ApiKey.objects.create(account=self.cust_acc, title="live")
        r = self.client.post("/api/wholesale/send/", {"to": "447700900123", "from": "MyBank", "text": "x" * 200},
                             HTTP_X_API_KEY=key.key)
        self.assertEqual(len(r.json()["ids"]), 2)
        self.assertEqual(sorted(M.objects.values_list("part_num", flat=True)), [1, 2])
        r = self.client.post("/api/wholesale/send/", {"to": "1", "from": "MyBank", "text": "x"}, HTTP_X_API_KEY=key.key)
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/wholesale/send/", {"to": "447700900123", "text": "x"}, HTTP_X_API_KEY="nope")
        self.assertEqual(r.status_code, 403)


class FakeVendor:
    """A tiny vendor SMSC: accepts binds, answers submit_sm with a HEX id, then sends a receipt
    using the DECIMAL form of that id (a real-world mismatch)."""

    def __init__(self, stat="DELIVRD", refuse=None):
        self.stat, self.refuse, self.received = stat, refuse, []
        self.n = 0x1000

    async def start(self):
        self.server = await asyncio.start_server(self.session, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def session(self, r, w):
        try:
            while True:
                length, cmd, status, seq = struct.unpack(">IIII", await r.readexactly(16))
                body = await r.readexactly(length - 16) if length > 16 else b""
                if cmd in (1, 2, 9):
                    w.write(pdu(cmd | 0x80000000, 0, seq, b"fake\0"))
                elif cmd == 0x15:
                    w.write(pdu(0x80000015, 0, seq))
                elif cmd == 4:
                    self.received.append(wpdu.parse_submit_raw(body))
                    if self.refuse:
                        w.write(pdu(0x80000004, self.refuse, seq, b"\0"))
                    else:
                        self.n += 1
                        w.write(pdu(0x80000004, 0, seq, format(self.n, "X").encode() + b"\0"))
                        text = f"id:{self.n} sub:001 dlvrd:001 submit date:2611270400 done date:2611270401 " \
                               f"stat:{self.stat} err:000 text:".encode()
                        dl = (b"\0" + bytes([1, 1]) + b"447700900123\0" + bytes([5, 0]) + b"MyBank\0"
                              + bytes([4, 0, 0]) + b"\0\0" + bytes([0, 0, 0, 0, len(text)]) + text)
                        w.write(pdu(5, 0, 99, dl))
                elif cmd == 6:
                    w.write(pdu(0x80000006, 0, seq))
                await w.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass


@override_settings(WHOLESALE_MAX_VENDOR_ATTEMPTS=4)
class EndToEndTests(TransactionTestCase):
    def setUp(self):
        world(self)
        self.su = SmppUser.objects.create(account=self.cust_acc, title="buyer", username="buyer", password="pw",
                                          role=SmppUser.WHOLESALE)
        from sms.smpp_server import SmppServer
        from wholesale.router import Router
        self.loop = asyncio.new_event_loop()
        self.bad, self.good = FakeVendor(refuse=0x45), FakeVendor()
        self.loop.run_until_complete(self.bad.start())
        self.loop.run_until_complete(self.good.start())
        VendorBind.objects.filter(pk=self.b1.pk).update(port=self.bad.port)  # cheapest vendor refuses
        VendorBind.objects.filter(pk=self.b2.pk).update(port=self.good.port)
        self.server = SmppServer("127.0.0.1", 0, poll=0.1)
        self.router = Router(poll=0.05, reload_every=0.5)
        self.loop.run_until_complete(self.server.start())
        self.loop.run_until_complete(self.router.start())
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        asyncio.run_coroutine_threadsafe(self.router.stop(), self.loop).result(2)
        asyncio.run_coroutine_threadsafe(self.server.stop(), self.loop).result(2)
        _time.sleep(0.2)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(2)

    def wait(self, cond, secs=8):
        end = _time.time() + secs
        while _time.time() < end:
            if cond():
                return True
            _time.sleep(0.05)
        return False

    def test_customer_submit_fails_over_and_gets_real_receipt_with_our_id(self):
        self.assertTrue(self.wait(lambda: VendorBind.objects.filter(bound=True).count() == 2))
        c = smpplib.client.Client("127.0.0.1", self.server.port, timeout=5, allow_unknown_opt_params=True)
        c.connect()
        got, sent = [], []
        c.set_message_received_handler(lambda pdu: got.append(pdu))
        c.set_message_sent_handler(lambda pdu: sent.append(pdu))
        c.bind_transceiver(system_id="buyer", password="pw")
        c.send_message(source_addr_ton=5, source_addr="MyBank", dest_addr_ton=1, destination_addr="447700900123",
                       short_message=b"Your code is 1234", registered_delivery=1)
        c.read_once()
        our_id = sent[0].message_id.decode()
        m = M.objects.get()
        self.assertEqual(m.message_id, our_id)
        self.assertTrue(self.wait(lambda: M.objects.get().status == M.DELIVERED))
        m.refresh_from_db()
        self.assertEqual((m.vendor, m.tried[0]["vendor"]), (self.v2, self.v1.pk))  # failed over
        self.assertEqual(self.good.received[0]["payload"], b"Your code is 1234")
        c.read_once()  # receipt deliver_sm
        self.assertEqual(got[0].esm_class & 4, 4)
        self.assertIn(f"id:{our_id}".encode(), got[0].short_message)
        self.assertIn(b"stat:DELIVRD", got[0].short_message)
        self.assertTrue(self.wait(lambda: M.objects.get().dlr_state == M.DLR_SENT))
        self.cust.refresh_from_db()
        self.assertEqual(self.cust.balance, Decimal("9.970"))
        c.unbind()
        c.disconnect()

    def test_tps_limit_throttles(self):
        SmppTestSettings.objects.create(smpp_user=self.su, tps=1)
        c = smpplib.client.Client("127.0.0.1", self.server.port, timeout=5, allow_unknown_opt_params=True)
        c.connect()
        sent = []
        c.set_message_sent_handler(lambda pdu: sent.append(pdu))
        c.bind_transmitter(system_id="buyer", password="pw")
        statuses = []
        for _ in range(3):
            c.send_message(source_addr="MyBank", destination_addr="447700900123", short_message=b"x")
            try:
                c.read_once()
                statuses.append(0)
            except smpplib.exceptions.PDUError as e:
                statuses.append(e.args[1])
        self.assertIn(0x58, statuses)
        c.disconnect()


class PageTests(TestCase):
    def setUp(self):
        world(self)
        from django.contrib.auth.models import User
        self.client.force_login(User.objects.create_superuser("boss", "b@example.com", "pw-long-enough"))

    def test_pages_render(self):
        m = engine.accept(self.cust_acc, fields()).message
        engine.vendor_accepted(m.pk, self.v1.pk, self.b1.pk, "1", Decimal("0.01"))
        for url in ["/wholesale/", "/wholesale/messages/", "/wholesale/messages/?export=csv", "/wholesale/quality/",
                    "/wholesale/routes/", "/wholesale/networks/", f"/wholesale/accounts/{self.cust_acc.pk}/",
                    f"/wholesale/accounts/{self.v1.pk}/", f"/accounts/{self.cust_acc.pk}/vendor-bind/new/",
                    f"/accounts/{self.cust_acc.pk}/?tab=wholesale"]:
            r = self.client.get(url, follow=True)
            self.assertEqual(r.status_code, 200, url)

    def test_account_actions(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        acc = Account.objects.create(title="New Buyer")
        url = f"/wholesale/accounts/{acc.pk}/"
        self.client.post(url, {"action": "enable_customer"})
        self.client.post(url, {"action": "topup", "amount": "25", "note": "Wire"})
        self.assertEqual(Customer.objects.get(account=acc).balance, Decimal("25"))
        self.client.post("/wholesale/networks/", {"csv": SimpleUploadedFile(
            "n.csv", b"country,network,mcc,mnc,prefix\nUnited Kingdom,Vodafone,234,15,447720 447721\n")})
        self.assertEqual(NetworkPrefix.objects.filter(network__mnc="15").count(), 2)
        self.client.post(url, {"action": "sell", "csv": SimpleUploadedFile(
            "r.csv", b"country,network,rate\nGB,,0.03\nUnited Kingdom,234-15,0.035\nNowhere,,1\n")})
        self.assertEqual(SellRate.objects.filter(account=acc).count(), 2)
        su = SmppUser.objects.create(account=acc, title="t", username="tst", password="p", role=SmppUser.WHOLESALE)
        self.client.post(f"/wholesale/smpp/{su.pk}/", {"tps": 5, "test_mode": "on", "test_delivered_pct": 90,
                                                       "test_price": "0"})
        self.assertTrue(SmppTestSettings.objects.get(smpp_user=su).test_mode)
        self.client.post("/wholesale/routes/", {"form": "plan", "title": "Premium", "mode": "priority"})
        plan = RoutePlan.objects.get()
        self.client.post("/wholesale/routes/", {"form": "entry", "plan": plan.pk, "vendor": self.v2.pk, "priority": 1})
        self.assertEqual(plan.entries.count(), 1)
