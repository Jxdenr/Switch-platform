"""Create and email last month's invoices (suppliers) and statements (clients).

Only accounts with "Email monthly statement / invoice automatically" ticked and a billing
email are sent to. Accounts with no traffic in the month are skipped. A document that was
already sent is never sent twice unless --resend is given.

    manage.py send_statements                 # last month, all accounts that opted in
    manage.py send_statements --month 2026-10 --account 12 --resend
    manage.py send_statements --loop          # stays running; sends on STATEMENT_SEND_DAY each month
"""
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from crm import statements as st
from crm.models import Account


class Command(BaseCommand):
    help = "Email monthly invoices and statements."

    def add_arguments(self, p):
        p.add_argument("--month", help="YYYY-MM (default: last month)")
        p.add_argument("--account", type=int)
        p.add_argument("--resend", action="store_true")
        p.add_argument("--dry-run", action="store_true", help="Create the documents but send nothing.")
        p.add_argument("--loop", action="store_true")

    def handle(self, *a, **o):
        if not o["loop"]:
            return self.run_once(o)
        while True:
            if timezone.localdate().day >= settings.STATEMENT_SEND_DAY:
                self.run_once({**o, "month": None})  # safe to repeat: sent documents are skipped
            time.sleep(3600)

    def run_once(self, o):
        first, last = st.month_bounds(o.get("month"))
        start, end = st.aware_range(first, last)
        accounts = Account.objects.filter(active=True)
        if o.get("account"):
            accounts = accounts.filter(pk=o["account"])
        else:
            accounts = accounts.filter(auto_statements=True).exclude(billing_email="")
        sent = skipped = failed = 0
        for acc in accounts:
            for side in st.sides(acc):
                t = st.totals(st.volumes(acc, side, start, end))
                if not t["sms"] and not t["calls"]:
                    continue  # no traffic, nothing to send
                doc = st.create_document(acc, st.kind_for(side), first)
                if doc.sent_at and not o.get("resend"):
                    skipped += 1
                    continue
                if o.get("dry_run"):
                    self.stdout.write(f"Would send {doc} ({doc.total} {doc.currency}) to {st.recipients(acc)}")
                    continue
                if st.send_document(doc):
                    sent += 1
                else:
                    failed += 1
                    self.stderr.write(f"{acc}: {doc.last_error}")
        if o.get("verbosity", 1):
            self.stdout.write(f"{first:%B %Y}: {sent} sent, {skipped} already sent, {failed} failed.")
