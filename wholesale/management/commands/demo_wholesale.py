"""Clearly fake wholesale example data (customers, vendors, rates, a week of messages) so every
wholesale screen has something to show. Prefixes and MCC/MNC here are illustrative only.
For demos and development only."""
import random
from datetime import timedelta
from decimal import Decimal

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import Country
from crm.models import Account, SmppUser
from wholesale.models import (ApiKey, BuyRate, Customer, Network, NetworkPrefix, OutboundMessage as M, RouteEntry,
                              RoutePlan, SellRate, SmppTestSettings, Vendor, VendorBind)

NETWORKS = [("EE", "234", "30", ["447700", "447701"]), ("O2", "234", "10", ["447702"]),
            ("Vodafone", "234", "15", ["447703"]), ("Three", "234", "20", ["447704"])]


class Command(BaseCommand):
    help = "Create example wholesale customers, vendors and traffic (demo only)."

    def add_arguments(self, parser):
        parser.add_argument("--portal", default="Demo Telecom", help="Portal to fill (created if missing).")

    def handle(self, *a, **o):
        from core.models import Tenant
        from core.tenancy import use_tenant
        tenant, _ = Tenant.objects.get_or_create(name=o["portal"], defaults={"company_name": f'{o["portal"]} Ltd'})
        with use_tenant(tenant):
            self._fill()

    def _fill(self):
        call_command("seed_lookups", verbosity=0)
        rnd = random.Random(11)
        uk = Country.objects.get(name="United Kingdom")
        nets = []
        for name, mcc, mnc, prefixes in NETWORKS:
            n, _ = Network.objects.get_or_create(mcc=mcc, mnc=mnc, defaults={"country": uk, "name": name})
            for p in prefixes:
                NetworkPrefix.objects.get_or_create(prefix=p, defaults={"network": n})
            nets.append((n, prefixes))

        def acc(title):
            return Account.objects.get_or_create(title=title)[0]

        direct, bulk, spare = acc("DirectLink Example"), acc("BulkRoute Example"), acc("Backup Vendor Example")
        for v, kind, rate, port in ((direct, "Direct", "0.0210", 2776), (bulk, "Direct (claimed)", "0.0140", 2777),
                                    (spare, "Premium", "0.0260", 2778)):
            Vendor.objects.get_or_create(account=v, defaults={"route_type": kind})
            BuyRate.objects.update_or_create(account=v, country=uk, network=None, defaults={"rate": Decimal(rate)})
            VendorBind.objects.get_or_create(account=v, title=f"{v.title} SMSC",
                                             defaults={"host": "203.0.113.20", "port": port, "username": "switch",
                                                       "password": "pw123", "tps": 50})
        plan, _ = RoutePlan.objects.get_or_create(title="Standard", defaults={"mode": RoutePlan.LCR,
                                                                              "min_delivery_pct": 85})
        prem, _ = RoutePlan.objects.get_or_create(title="Premium", defaults={"mode": RoutePlan.PRIORITY})
        for p, v, prio in ((plan, bulk, 1), (plan, direct, 2), (plan, spare, 3), (prem, direct, 1), (prem, spare, 2)):
            RouteEntry.objects.get_or_create(plan=p, vendor=v, country=uk, defaults={"priority": prio})

        cust_rows = [("Northwind Messaging Example", plan, "0.0300", Decimal("412.50"), 0, Customer.ACCEPTED),
                     ("Fintech OTP Example", prem, "0.0340", Decimal("-120.00"), 500, Customer.DELIVERED),
                     ("Trial Customer Example", plan, "0.0320", Decimal("4.20"), 0, Customer.ACCEPTED)]
        customers = []
        for i, (title, p, sell, bal, limit, charge) in enumerate(cust_rows):
            a = acc(title)
            c, _ = Customer.objects.get_or_create(account=a, defaults={
                "route_plan": p, "credit_limit": limit, "charge_on": charge, "low_balance_alert": 20})
            Customer.objects.filter(pk=c.pk).update(balance=bal)
            SellRate.objects.update_or_create(account=a, country=uk, network=None, defaults={"rate": Decimal(sell)})
            su, _ = SmppUser.objects.get_or_create(username=f"ws{i}", defaults={
                "account": a, "title": f"{title.split()[0]} live", "password": "demopass",
                "role": SmppUser.WHOLESALE})
            SmppTestSettings.objects.get_or_create(smpp_user=su, defaults={"tps": 50})
            customers.append((a, Decimal(sell), charge))
        trial = customers[2][0]
        ApiKey.objects.get_or_create(account=trial, title="Sandbox (test)", defaults={
            "test_mode": True, "test_delivered_pct": 95, "dlr_url": "https://trial.example.com/dlr"})

        if M.objects.exists():
            self.stdout.write("Wholesale messages already exist; left them as they are.")
            return
        now = timezone.now()
        vendor_profile = {  # delivered share, receipt delay (s): real-looking vs suspicious
            direct.pk: (0.93, lambda: rnd.uniform(2.5, 14)), bulk.pk: (1.0, lambda: rnd.uniform(0.2, 0.5)),
            spare.pk: (0.88, lambda: rnd.uniform(3, 25))}
        rows = []
        for k in range(1800):
            a, sell, charge = rnd.choices(customers, weights=[6, 3, 1])[0]
            when = now - timedelta(minutes=rnd.randint(1, 7 * 24 * 60))
            net, prefixes = rnd.choice(nets)
            dest = rnd.choice(prefixes) + "900" + f"{rnd.randint(0, 999):03d}"
            m = M(created_at=when, customer=a, source=rnd.choice(["ShopCo", "Verify", "MyBank", "447700900000"]),
                  destination=dest, body=f"Your code is {rnd.randint(100000, 999999)}", country=uk, network=net,
                  sell_rate=sell, charge_on=charge, registered_delivery=1, payload=b"")
            if k % 37 == 0:
                m.status, m.reason = M.REFUSED, rnd.choice(["Not enough credit", "Blocked by rule: Block text containing: bit.ly",
                                                             "Sender ID PayPal is not on this customer's allowed list"])
                rows.append(m)
                continue
            v = rnd.choices([bulk, direct, spare], weights=[5, 4, 1])[0]
            ok_share, delay = vendor_profile[v.pk]
            m.vendor = v
            m.buy_rate = BuyRate.objects.get(account=v).rate
            m.vendor_message_id = f"{rnd.getrandbits(40):x}"
            m.submitted_at = when + timedelta(milliseconds=rnd.randint(30, 400))
            if when > now - timedelta(minutes=3):
                m.status = M.SUBMITTED
            elif v == spare and rnd.random() < 0.03:
                m.status, m.stat, m.no_receipt, m.reason = M.UNKNOWN, "UNKNOWN", True, "No receipt from route within 72h"
            else:
                d = rnd.random() < ok_share
                m.status, m.stat = (M.DELIVERED, "DELIVRD") if d else rnd.choice(
                    [(M.UNDELIVERED, "UNDELIV"), (M.EXPIRED, "EXPIRED"), (M.REJECTED, "REJECTD")])
                m.err = "000" if d else rnd.choice(["001", "006", "00B"])
                m.dlr_received_at = m.done_at = m.submitted_at + timedelta(seconds=delay())
            m.charged = sell if (charge == Customer.ACCEPTED or m.status == M.DELIVERED) else Decimal(0)
            m.dlr_state = M.DLR_SENT if m.status in M.FINAL else M.DLR_NONE
            rows.append(m)
        M.objects.bulk_create(rows, batch_size=500)
        self.stdout.write(self.style.SUCCESS(f"Created {len(rows)} wholesale messages."))
