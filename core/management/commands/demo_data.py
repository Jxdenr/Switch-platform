"""Fill a fresh database with clearly fake example data so every screen has something
to show. Uses Ofcom's drama range (+44 7700 900xxx), which is never assigned to real phones."""
import random
from datetime import timedelta
from decimal import Decimal

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import Country, Currency, PaymentTerms, TimeOfDay
from crm.models import Account, HttpEndpoint, HttpUser, SmppUser, Smsc
from inventory import services
from inventory.models import BillingGroup, BillingGroupRate, Number
from sms.models import Message


class Command(BaseCommand):
    help = "Create example suppliers, clients, numbers and SMS (for demos and development only)."

    def add_arguments(self, parser):
        parser.add_argument("--portal", default="Demo Telecom", help="Portal to fill (created if missing).")

    def handle(self, *args, **opts):
        from core.models import Tenant
        from core.tenancy import use_tenant
        call_command("seed_lookups")
        tenant, _ = Tenant.objects.get_or_create(name=opts["portal"], defaults={
            "company_name": f'{opts["portal"]} Ltd',
            "company_details": "1 Example Street, London\nBank: Example Bank · 00-00-00 · 00000000"})
        with use_tenant(tenant):
            self._fill()
        self.stdout.write(f"Demo data is in the portal \"{tenant}\". Give it an owner from the Platform page, "
                          f"or: manage.py create_portal \"{tenant}\" you@example.com")

    def _fill(self):
        rnd = random.Random(7)
        gbp, uk = Currency.objects.get(code="GBP"), Country.objects.get(name="United Kingdom")
        t3045, all_times = PaymentTerms.objects.get(title="30/45"), TimeOfDay.objects.get(title="All Times")
        suppliers = []
        for i, name in enumerate(["Example Wholesale", "Sample Carrier", "Demo Telecom"]):
            s, _ = Account.objects.get_or_create(title=name, defaults={"is_supplier": True, "currency": gbp,
                                                                       "payment_terms": t3045})
            SmppUser.objects.get_or_create(username=f"demo{i}", defaults={"account": s, "title": f"{name} bind",
                                                                            "password": "demopass"})
            if i == 0:
                Smsc.objects.get_or_create(account=s, title="Outbound to Example Wholesale",
                                           defaults={"ip": "203.0.113.10", "username": "switch", "password": "pw123"})
            Account.objects.filter(pk=s.pk).update(billing_email=f"billing@{name.split()[0].lower()}.example.com",
                                                   auto_statements=True)
            if i == 2:
                HttpUser.objects.get_or_create(account=s, title="HTTP API", defaults={"ip": "198.51.100.30"})
            suppliers.append(s)
        clients = []
        for name in ["Client Alpha", "Client Bravo", "Client Charlie"]:
            slug = name.split()[1].lower()
            c, _ = Account.objects.get_or_create(title=name, defaults={
                "is_client": True, "currency": gbp, "payment_terms": t3045,
                "billing_email": f"accounts@{slug}.example.com", "auto_statements": name != "Client Bravo"})
            if name == "Client Charlie":  # receives its SMS over SMPP instead of HTTP
                SmppUser.objects.get_or_create(username="charlie", defaults={
                    "account": c, "title": "Charlie SMPP receiver", "password": "demopw", "role": "client"})
            else:
                HttpEndpoint.objects.get_or_create(account=c, title="Inbound webhook",
                                                   defaults={"url": "https://example.com/sms-webhook"})
            clients.append(c)
        start = 447700900000
        for gi, s in enumerate(suppliers):
            g, _ = BillingGroup.objects.get_or_create(supplier=s, title=f"UK Mobile {'ABC'[gi]}",
                                                      defaults={"country": uk, "currency": gbp, "routing_type": "both",
                                                                "network": "Mobile"})
            for ch, rin, rout in [("sms", "0.02000", "0.01500"), ("voice", "0.06000", "0.04500")]:
                BillingGroupRate.objects.get_or_create(billing_group=g, channel=ch, slot=1, time_of_day=all_times,
                                                       defaults={"payment_terms": t3045, "rate_in": Decimal(rin),
                                                                 "rate_out": Decimal(rout)})
            nums = [str(start + gi * 300 + k) for k in range(300)]
            services.add_numbers(g, nums, via="system")
            group_nums = list(Number.objects.filter(billing_group=g)[:240])
            for ci, c in enumerate(clients):
                services.allocate(group_nums[ci * 80:(ci + 1) * 80], c, t3045, via="system")
        self._calls(rnd)
        self._documents()
        self._extras(rnd, suppliers, clients, t3045, all_times)
        self._rules(suppliers, clients)
        if Message.objects.filter(currency="GBP").exists():
            self.stdout.write("Messages already present, skipping.")
            return
        from sms.routing import receive
        senders = ["ExampleCo", "DemoBank", "ShopCo", "12345", "TestApp"]
        now = timezone.now()
        allocated = list(Number.objects.filter(allocations__ends_at__isnull=True)[:240])
        for k in range(1500):
            n = rnd.choice(allocated)
            m = receive(source=rnd.choice(senders), destination=n.number,
                        body=f"Example message {k} for the demo", smpp_username=f"demo{rnd.randint(0, 2)}",
                        received_at=now - timedelta(minutes=rnd.randint(0, 60 * 24 * 30)))
            m.status = Message.FORWARDED if rnd.random() > 0.03 else Message.FAILED
            if m.status == Message.FAILED:
                m.last_error = "HTTP 503: Service Unavailable"
            m.save()
        self._documents()
        self.stdout.write(self.style.SUCCESS("Demo data ready."))

    def _extras(self, rnd, suppliers, clients, terms, all_times):
        """A euro-priced range (so the dashboard shows currencies kept apart) and some traffic to
        numbers nobody has been given (so 'Unrouted' has something in it)."""
        from sms.routing import receive
        if Message.objects.filter(currency="EUR").exists():
            return
        eur = Currency.objects.filter(code="EUR").first()
        nl = Country.objects.filter(name="Netherlands").first()
        if not eur or not nl:
            return
        g, _ = BillingGroup.objects.get_or_create(supplier=suppliers[1], title="NL Mobile", defaults={
            "country": nl, "currency": eur, "routing_type": "sms", "network": "Mobile"})
        BillingGroupRate.objects.get_or_create(billing_group=g, channel="sms", slot=1, time_of_day=all_times,
                                               defaults={"payment_terms": terms, "rate_in": Decimal("0.03000"),
                                                         "rate_out": Decimal("0.02200")})
        services.add_numbers(g, [str(31612000000 + k) for k in range(100)], via="system")
        services.allocate(list(Number.objects.filter(billing_group=g)[:60]), clients[1], terms, via="system")
        now = timezone.now()
        nl_nums = list(Number.objects.filter(billing_group=g)[:60])
        for k in range(220):
            m = receive(source="ShopCo", destination=rnd.choice(nl_nums).number, body=f"NL demo {k}",
                        smpp_username="demo1", received_at=now - timedelta(minutes=rnd.randint(0, 60 * 24 * 30)))
            m.status = Message.FORWARDED
            m.save()
        free = list(Number.objects.filter(allocations__isnull=True).exclude(billing_group=g)[:30])
        for k in range(45):
            receive(source="DemoBank", destination=rnd.choice(free).number, body=f"Unrouted demo {k}",
                    smpp_username="demo0", received_at=now - timedelta(minutes=rnd.randint(0, 60 * 24 * 14)))

    def _rules(self, suppliers, clients):
        from sms.models import BlockRule
        if BlockRule.objects.exists():
            return
        g = BillingGroup.objects.filter(supplier=suppliers[0]).first()
        BlockRule.objects.create(scope="all", rule_type="sender", value="SpamCo", reason="Known spam sender")
        BlockRule.objects.create(scope="supplier", account=suppliers[2], rule_type="limit", value="30",
                                 reason="Pumping seen in October")
        BlockRule.objects.create(scope="client", account=clients[1], rule_type="prefix", value="447700900599",
                                 reason="Client asked to stop this block")
        BlockRule.objects.create(scope="group", billing_group=g, rule_type="limit", value="20",
                                 reason="Set on billing group")

    def _calls(self, rnd):
        from voice.billing import record_call
        from voice.models import Call
        if Call.objects.exists():
            return
        BillingGroup.objects.update(billing_increment_first=60, billing_increment_next=60)
        now = timezone.now()
        nums = list(Number.objects.filter(allocations__channel="voice", allocations__ends_at__isnull=True)
                    .order_by("?").values_list("number", flat=True)[:500])
        causes = [("NORMAL_CLEARING", 16, 0.7), ("NO_ANSWER", 19, 0.12), ("USER_BUSY", 17, 0.1),
                  ("CALL_REJECTED", 21, 0.05), ("NORMAL_TEMPORARY_FAILURE", 41, 0.03)]
        for k in range(600):
            cause, q850 = rnd.choices([(c, q) for c, q, _ in causes], weights=[w for *_, w in causes])[0]
            start = now - timedelta(minutes=rnd.randint(0, 60 * 24 * 30))
            billsec = int(rnd.expovariate(1 / 95)) + 3 if cause == "NORMAL_CLEARING" else 0
            ring = rnd.randint(2, 12)
            record_call(uuid=f"demo-{k:05d}", number_called=rnd.choice(nums),
                        caller_id=f"4420790{rnd.randint(0, 99999):05d}", start=start,
                        answer=start + timedelta(seconds=ring) if billsec else None,
                        end=start + timedelta(seconds=ring + billsec), duration=ring + billsec, billsec=billsec,
                        pdd_ms=rnd.randint(800, 3500), hangup_cause=cause, hangup_cause_q850=q850,
                        from_ip="198.51.100.20", mos_in=Decimal(str(round(rnd.uniform(3.6, 4.4), 2))) if billsec else None)

    def _documents(self):
        """Last month's invoices and statements, shown as already sent."""
        from crm import statements as st
        from crm.models import Document
        if Document.objects.exists() or not Message.objects.exists():
            return
        first, _ = st.month_bounds(None)
        for acc in Account.objects.exclude(billing_email=""):
            for side in st.sides(acc):
                doc = st.create_document(acc, st.kind_for(side), first)
                if acc.auto_statements and doc.total:
                    Document.objects.filter(pk=doc.pk).update(
                        sent_at=timezone.now().replace(day=1, hour=6, minute=0), sent_to=acc.billing_email)
