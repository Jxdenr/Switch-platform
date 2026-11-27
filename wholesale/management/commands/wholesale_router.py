"""Run the wholesale router: vendor binds, sending, failover, receipts. Run exactly one copy."""
import asyncio
import logging

from django.core.management.base import BaseCommand

from wholesale.models import VendorBind
from wholesale.router import Router


class Command(BaseCommand):
    help = "Bind to vendors, send queued wholesale traffic and take in delivery receipts."

    def handle(self, *a, **o):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
        VendorBind.objects.update(bound=False)

        async def main():
            await Router().start()
            await asyncio.Event().wait()

        asyncio.run(main())
