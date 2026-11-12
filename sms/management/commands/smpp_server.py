"""Run the built-in SMPP server (suppliers and clients bind to us here)."""
import asyncio
import logging

from django.conf import settings
from django.core.management.base import BaseCommand

from crm.models import SmppUser
from sms.smpp_server import SmppServer


class Command(BaseCommand):
    help = "Run the SMPP server that suppliers and clients bind to."

    def add_arguments(self, p):
        p.add_argument("--port", type=int, default=settings.SMPP_SERVER_PORT)

    def handle(self, *a, **o):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        SmppUser.objects.update(bound=False)  # nobody is bound until they connect to this process

        async def main():
            await SmppServer(settings.SMPP_SERVER_HOST, o["port"]).start()
            await asyncio.Event().wait()

        asyncio.run(main())
