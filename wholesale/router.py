"""The vendor side: we bind out to each vendor's SMSC, send queued messages, fail over when a
vendor refuses, and take in the vendors' receipts.

Run exactly one copy: `manage.py wholesale_router`. It
  * keeps one link per enabled vendor bind (or two for transmitter + receiver binds), with
    enquire_link keep-alives and reconnect with back-off,
  * sends each queued message to the best vendor that is up and under its TPS and window,
  * on a refusal or no answer, moves on to the next vendor (throttling retries the same one),
  * matches receipts to our messages and queues them for the customer,
  * every minute marks unanswered messages UNKNOWN after the receipt timeout.
"""
import asyncio
import logging
import struct

from asgiref.sync import sync_to_async
from django.utils import timezone

from sms.smpp_server import cstr, pdu
from . import engine, pdu as wpdu
from .models import Customer, OutboundMessage as M, VendorBind

logger = logging.getLogger("wholesale.router")

BIND_RX, BIND_TX, BIND_TRX = 0x01, 0x02, 0x09
SUBMIT_SM, DELIVER_SM, UNBIND, ENQUIRE_LINK, GENERIC_NACK, RESP = 0x04, 0x05, 0x06, 0x15, 0x80000000, 0x80000000
RTHROTTLED, RMSGQFUL = 0x58, 0x14
RETRY_SAME = {RTHROTTLED, RMSGQFUL}
SUBMIT_TIMEOUT = 30
KEEPALIVE = 30


class Link:
    """One SMPP connection to a vendor."""

    def __init__(self, router, bind: VendorBind, mode):
        self.router, self.bind, self.mode = router, bind, mode
        self.reader = self.writer = None
        self.seq = 0
        self.pending = {}  # seq -> future
        self.up = False
        self.tokens, self.last = float(bind.tps), 0.0
        self.task = None
        self.stopping = False

    @property
    def can_send(self):
        return self.up and self.mode in (BIND_TX, BIND_TRX)

    def has_capacity(self):
        if not self.can_send or len(self.pending) >= max(1, self.bind.window):
            return False
        now = asyncio.get_running_loop().time()
        self.tokens = min(float(self.bind.tps), self.tokens + (now - self.last) * self.bind.tps)
        self.last = now
        return self.tokens >= 1

    def next_seq(self):
        self.seq = self.seq % 0x7FFFFFFF + 1
        return self.seq

    async def send(self, data):
        self.writer.write(data)
        await self.writer.drain()

    async def request(self, cmd, body=b"", timeout=SUBMIT_TIMEOUT):
        seq = self.next_seq()
        fut = asyncio.get_running_loop().create_future()
        self.pending[seq] = fut
        await self.send(pdu(cmd, 0, seq, body))
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self.pending.pop(seq, None)

    async def run(self):
        delay = 1
        while not self.stopping:
            try:
                await self.connect()
                delay = 1
                await self.read_loop()
            except (OSError, asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError) as e:
                await set_status(self.bind.pk, False, f"{type(e).__name__}: {e}"[:200])
            except Exception as e:  # keep the link alive whatever happens
                logger.exception("Vendor link %s crashed", self.bind)
                await set_status(self.bind.pk, False, str(e)[:200])
            self.drop()
            if self.stopping:
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def connect(self):
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(self.bind.host, self.bind.port), 10)
        reader_task = asyncio.get_running_loop().create_task(self.read_loop())
        self._reader_task = reader_task
        body = (self.bind.username.encode() + b"\0" + self.bind.password.encode() + b"\0"
                + self.bind.system_type.encode() + b"\0" + bytes([0x34, 0, 0]) + b"\0")
        status, _ = await self.request(self.mode, body, timeout=10)
        if status != 0:
            raise ConnectionError(f"bind refused 0x{status:02X}")
        self.up = True
        self.tokens = float(self.bind.tps)
        self.last = asyncio.get_running_loop().time()
        await set_status(self.bind.pk, True, "")
        logger.info("Bound to vendor %s (%s)", self.bind, {1: "rx", 2: "tx", 9: "trx"}[self.mode])
        self.keepalive = asyncio.get_running_loop().create_task(self.keep_alive())

    async def read_loop(self):
        if getattr(self, "_reader_task", None) and asyncio.current_task() is not self._reader_task:
            await self._reader_task  # the reader started in connect() does the reading
            return
        while True:
            head = await self.reader.readexactly(16)
            length, cmd, status, seq = struct.unpack(">IIII", head)
            if length < 16 or length > 64 * 1024:
                raise ConnectionError("bad PDU length")
            body = await self.reader.readexactly(length - 16) if length > 16 else b""
            if cmd & RESP or cmd == GENERIC_NACK:
                fut = self.pending.get(seq)
                if fut and not fut.done():
                    fut.set_result((status, body))
            elif cmd == ENQUIRE_LINK:
                await self.send(pdu(ENQUIRE_LINK | RESP, 0, seq))
            elif cmd == DELIVER_SM:
                await self.on_deliver(seq, body)
            elif cmd == UNBIND:
                await self.send(pdu(UNBIND | RESP, 0, seq))
                raise ConnectionError("vendor unbound")
            else:
                await self.send(pdu(GENERIC_NACK, 0x03, seq))

    async def on_deliver(self, seq, body):
        try:
            esm, sm, tlvs = wpdu.parse_deliver(body)
            if wpdu.is_receipt(esm, sm, tlvs):
                receipt = wpdu.parse_receipt(esm, sm, tlvs)
                if not await store_receipt(self.bind.account_id, self.bind.pk, receipt):
                    asyncio.get_running_loop().create_task(self.retry_receipt(receipt))
            else:
                logger.info("Ignoring non-receipt deliver_sm from vendor %s", self.bind)
        except Exception:
            logger.exception("Bad deliver_sm from vendor %s", self.bind)
        # Always acknowledge, or the vendor resends the same receipt forever.
        await self.send(pdu(DELIVER_SM | RESP, 0, seq, b"\0"))

    async def retry_receipt(self, receipt, tries=10, wait=3.0):
        """A receipt can beat our own record of the vendor's message ID. Try again for ~30s."""
        for _ in range(tries):
            await asyncio.sleep(wait)
            if await store_receipt(self.bind.account_id, self.bind.pk, receipt):
                return
        await unmatched(self.bind.pk, receipt["id"])

    async def keep_alive(self):
        missed = 0
        while self.up:
            await asyncio.sleep(KEEPALIVE)
            try:
                await self.request(ENQUIRE_LINK, timeout=10)
                missed = 0
            except asyncio.TimeoutError:
                missed += 1
                if missed >= 2 and self.writer:
                    self.writer.close()
                    return
            except Exception:
                return

    async def submit(self, body):
        self.tokens -= 1
        return await self.request(SUBMIT_SM, body)

    def drop(self):
        self.up = False
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("link dropped"))
        self.pending.clear()
        for t in (getattr(self, "keepalive", None), getattr(self, "_reader_task", None)):
            if t and t is not asyncio.current_task():
                t.cancel()
        self._reader_task = None
        if self.writer:
            try:
                self.writer.close()
            except Exception:
                pass
        self.writer = None


# ---------- database helpers ----------

@sync_to_async
def set_status(bind_id, bound, error):
    VendorBind.objects.filter(pk=bind_id).update(bound=bound, last_status_at=timezone.now(),
                                                 **({"last_error": error} if error or bound else {}))


@sync_to_async
def store_receipt(vendor_id, bind_id, receipt):
    return engine.vendor_receipt(vendor_id, bind_id, receipt)


@sync_to_async
def unmatched(bind_id, dlr_id):
    engine.count_unmatched(bind_id, dlr_id)


@sync_to_async
def load_binds():
    return list(VendorBind.objects.filter(enabled=True, account__wholesale_vendor__enabled=True))


@sync_to_async
def due_messages(exclude, limit=200):
    now = timezone.now()
    return list(M.objects.filter(status=M.QUEUED, test=False, next_attempt_at__lte=now)
                .exclude(pk__in=exclude).select_related("customer").order_by("next_attempt_at")[:limit])


@sync_to_async
def plan_for(msg):
    """Vendors to try for this message, best first, skipping ones that already refused it.
    Parts of one long message stay on the vendor that took the first part."""
    customer = Customer.objects.select_related("route_plan").get(account_id=msg.customer_id)
    cands = engine.candidates(customer, msg.country, msg.network, msg.sell_rate, exclude=engine.tried_vendors(msg))
    if msg.part_ref is not None:
        sibling = (M.objects.filter(customer_id=msg.customer_id, destination=msg.destination, source=msg.source,
                                    part_ref=msg.part_ref, vendor__isnull=False,
                                    created_at__gte=msg.created_at - timezone.timedelta(minutes=10))
                   .exclude(pk=msg.pk).values_list("vendor_id", flat=True).first())
        if sibling:
            cands.sort(key=lambda c: c.vendor_id != sibling)
    return cands


@sync_to_async
def give_up(msg_pk, reason):
    engine.reject(M.objects.get(pk=msg_pk), reason, stat="UNDELIV")


@sync_to_async
def accepted(msg_pk, vendor_id, bind_id, vendor_msg_id, buy):
    engine.vendor_accepted(msg_pk, vendor_id, bind_id, vendor_msg_id, buy)


@sync_to_async
def failed(msg_pk, vendor_id, bind_id, error, retry_same=False):
    engine.vendor_failed(msg_pk, vendor_id, bind_id, error, retry_same)


@sync_to_async
def sweep():
    return engine.expire_unanswered()


class Router:
    def __init__(self, poll=0.2, reload_every=30):
        self.links = {}  # (bind id, mode) -> Link
        self.inflight = set()
        self.poll, self.reload_every = poll, reload_every

    async def start(self):
        self.tasks = [asyncio.get_running_loop().create_task(t) for t in
                      (self.manage_links(), self.dispatch_loop(), self.sweep_loop())]

    async def stop(self):
        for t in self.tasks:
            t.cancel()
        for link in self.links.values():
            link.stopping = True
            link.drop()
            if link.task:
                link.task.cancel()

    async def manage_links(self):
        while True:
            try:
                binds = {b.pk: b for b in await load_binds()}
                wanted = set()
                for b in binds.values():
                    modes = [BIND_TRX] if b.bind_mode == VendorBind.TRX else [BIND_TX, BIND_RX]
                    for mode in modes:
                        key = (b.pk, mode)
                        wanted.add(key)
                        link = self.links.get(key)
                        if link and _changed(link.bind, b):
                            link.stopping = True
                            link.drop()
                            link.task.cancel()
                            link = None
                        if not link:
                            link = self.links[key] = Link(self, b, mode)
                            link.task = asyncio.get_running_loop().create_task(link.run())
                        else:
                            link.bind.tps, link.bind.window = b.tps, b.window
                for key in set(self.links) - wanted:
                    link = self.links.pop(key)
                    link.stopping = True
                    link.drop()
                    link.task.cancel()
                    await set_status(key[0], False, "")
            except Exception:
                logger.exception("Reloading vendor binds failed")
            await asyncio.sleep(self.reload_every)

    def link_for(self, vendor_id):
        for link in self.links.values():
            if link.bind.account_id == vendor_id and link.has_capacity():
                return link
        return None

    def vendor_up(self, vendor_id):
        return any(link.can_send and link.bind.account_id == vendor_id for link in self.links.values())

    async def dispatch_loop(self):
        while True:
            try:
                await self.dispatch_once()
            except Exception:
                logger.exception("Dispatch failed")
            await asyncio.sleep(self.poll)

    async def dispatch_once(self):
        for msg in await due_messages(list(self.inflight)):
            cands = await plan_for(msg)
            if not cands:
                await give_up(msg.pk, "No route left: every vendor refused it or none is available")
                continue
            for c in cands:
                link = self.link_for(c.vendor_id)
                if link:
                    self.inflight.add(msg.pk)
                    asyncio.get_running_loop().create_task(self.send(link, msg, c))
                    break
            # Nobody has capacity right now: it stays queued until the queue timeout.

    async def send(self, link, msg, cand):
        b = link.bind
        ton, npi = wpdu.auto_ton(msg.source)
        if b.src_ton is not None:
            ton = b.src_ton
        if b.src_npi is not None:
            npi = b.src_npi
        body = wpdu.submit_sm(msg.source, ton, npi, msg.destination, msg.esm_class, msg.data_coding,
                              bytes(msg.payload), registered=1)
        try:
            status, resp = await link.submit(body)
            if status == 0:
                vendor_id, _ = cstr(resp, 0) if resp else ("", 0)
                await accepted(msg.pk, b.account_id, b.pk, vendor_id, cand.buy_rate)
            else:
                await failed(msg.pk, b.account_id, b.pk, f"Vendor refused 0x{status:02X}", status in RETRY_SAME)
        except (asyncio.TimeoutError, ConnectionError) as e:
            # No answer: the vendor may or may not have it. Failing over risks a duplicate, but
            # holding it risks losing it; for OTP traffic a duplicate is the lesser harm.
            await failed(msg.pk, b.account_id, b.pk, f"No answer from vendor ({type(e).__name__})")
        except Exception as e:
            logger.exception("Sending %s to %s failed", msg.message_id, b)
            await failed(msg.pk, b.account_id, b.pk, str(e)[:100])
        finally:
            self.inflight.discard(msg.pk)

    async def sweep_loop(self):
        while True:
            try:
                n = await sweep()
                if n:
                    logger.info("Closed %s messages with no receipt / no route in time", n)
            except Exception:
                logger.exception("Sweep failed")
            await asyncio.sleep(60)


def _changed(old, new):
    return any(getattr(old, f) != getattr(new, f) for f in
               ("host", "port", "username", "password", "system_type", "bind_mode", "src_ton", "src_npi"))
