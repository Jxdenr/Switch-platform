"""Push SMPP binds from the CRM into Jasmin, and read back which binds are up.

What it configures:
  * HTTP connector `switchapp` -> our /ingest/jasmin/ endpoint.
  * Each SMSC (we bind to the supplier) -> smppccm connector `smsc<id>`, started if enabled.
    Inbound SMS (deliver_sm) from those binds reach us via a default MO route to `switchapp`.
  * Binds TO us (SMPP users, suppliers and clients) are NOT in Jasmin: they connect to the
    app's own SMPP server (`manage.py smpp_server`), which keeps their bind status itself.

Run `manage.py sync_jasmin --status` on a schedule (every minute) to refresh bind status.
NOTE: verify against your Jasmin version on a test bind before relying on it in production.
"""
from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from crm.models import Smsc
from sms.jcli import JCli


class Command(BaseCommand):
    help = "Sync SMPP binds to Jasmin (or with --status, refresh bind status only)."

    def add_arguments(self, parser):
        parser.add_argument("--status", action="store_true")
        parser.add_argument("--dry-run", action="store_true", help="Print jCli commands without connecting.")

    def handle(self, *args, **o):
        if o["dry_run"]:
            for line in self.plan():
                self.stdout.write(line)
            return
        j = JCli(settings.JASMIN_JCLI_HOST, settings.JASMIN_JCLI_PORT,
                 settings.JASMIN_JCLI_USER, settings.JASMIN_JCLI_PASSWORD)
        try:
            if not o["status"]:
                self.apply(j)
            self.refresh_status(j)
        finally:
            j.close()

    def plan(self):
        lines = [f"httpccm -a / cid switchapp / url {self.ingest_url()} / method POST",
                 "morouter -a / type DefaultRoute / connector http(switchapp)"]
        for s in Smsc.objects.all():
            lines.append(f"smppccm -a / cid {s.connector_id} / host {s.ip} / port {s.port} / username {s.username} "
                         f"/ password *** / bind {'transceiver' if s.transceiver else 'receiver'}")
        return lines

    def ingest_url(self):
        return f"{settings.JASMIN_MO_CALLBACK_URL}?token={settings.INGEST_TOKEN}"

    def apply(self, j):
        if "switchapp" not in j.run("httpccm -l"):
            j.block("httpccm -a", {"cid": "switchapp", "url": self.ingest_url(), "method": "POST"})
        if "DefaultRoute" not in j.run("morouter -l"):
            j.block("morouter -a", {"type": "DefaultRoute", "connector": "http(switchapp)"})
        existing = j.run("smppccm -l")
        for s in Smsc.objects.all():
            fields = {"host": s.ip, "port": s.port, "username": s.username, "password": s.password,
                      "bind": "transceiver" if s.transceiver else "receiver"}
            if s.system_type:
                fields["systype"] = s.system_type
            if s.src_ton is not None:
                fields["src_ton"] = s.src_ton
            if s.dst_ton is not None:
                fields["dst_ton"] = s.dst_ton
            if s.connector_id in existing:
                j.block(f"smppccm -u {s.connector_id}", fields)
            else:
                j.block("smppccm -a", {"cid": s.connector_id, **fields})
            j.run(f"smppccm -{'1' if s.enabled else '0'} {s.connector_id}")
        j.run("persist")
        self.stdout.write(self.style.SUCCESS("Jasmin configuration synced."))

    def refresh_status(self, j):
        now = timezone.now()
        listing = j.run("smppccm -l")
        for s in Smsc.objects.all():
            line = next((ln for ln in listing.splitlines() if s.connector_id in ln), "")
            Smsc.objects.filter(pk=s.pk).update(bound="BOUND" in line.upper(), last_status_at=now)
        self.stdout.write("Bind status refreshed.")
