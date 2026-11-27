"""Push received SMS to client endpoints, retrying failures with back-off.
Run as a long-lived worker: `manage.py forward_pending --loop`."""
import time
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db.models import F, Q
from django.utils import timezone

from sms.models import Message
from sms.routing import forward


class Command(BaseCommand):
    help = "Forward received and failed messages to client HTTP endpoints."

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="Keep running, polling every second.")

    def handle(self, *args, **opts):
        while True:
            n = self.run_once()
            if not opts["loop"]:
                self.stdout.write(f"Processed {n} messages.")
                return
            if not n:
                time.sleep(1)

    def run_once(self):
        now = timezone.now()
        qs = (Message.objects.filter(http_endpoint__isnull=False, forward_attempts__lt=settings.FORWARD_MAX_ATTEMPTS)
              .filter(Q(status=Message.RECEIVED) | Q(status=Message.FAILED))
              .select_related("http_endpoint").order_by("received_at")[:200])
        done = 0
        for m in qs:
            # back-off: wait 2^attempts minutes between retries
            last = m.last_attempt_at or m.received_at
            if m.status == Message.FAILED and last + timedelta(minutes=2 ** m.forward_attempts) > now:
                continue
            forward(m)
            done += 1
        from wholesale.engine import push_http_dlrs
        return done + push_http_dlrs()
