"""Built-in SMPP 3.4 server. Everyone who binds TO us connects here.

* Suppliers bind (transmitter or transceiver) and send SMS for our numbers with submit_sm.
  Each one is recorded and routed exactly like SMS that arrive any other way.
* Clients bind (receiver or transceiver) and receive the SMS for their allocated numbers as
  deliver_sm. Delivery is retried with the same back-off as HTTP forwarding.

Binds where WE connect out to a supplier's SMSC still go through Jasmin (see sync_jasmin).
Run with `manage.py smpp_server`. Bind status shown in the app is kept up to date from here.

* Wholesale customers bind (any mode) and send traffic to be routed to vendors. Their submit_sm
  is answered with our message ID; the vendor's real receipt comes back to them as deliver_sm
  (see wholesale/). Each bind has a messages-per-second limit; over it they get ESME_RTHROTTLED.

Known limits (v1): long messages split in parts (UDH) are stored one record per part, and we
do not send delivery receipts back to suppliers yet.
"""
import asyncio
import logging
import string
import struct
from datetime import timedelta

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger("smpp")

BIND_RX, BIND_TX, BIND_TRX = 0x01, 0x02, 0x09
SUBMIT_SM, DELIVER_SM, UNBIND, ENQUIRE_LINK, GENERIC_NACK = 0x04, 0x05, 0x06, 0x15, 0x80000000
RESP = 0x80000000
ROK, RINVCMDID, RALYBND, RINVBNDSTS, RSYSERR, RBINDFAIL, RINVPASWD, RINVSYSID, RSUBMITFAIL = (
    0x00, 0x03, 0x05, 0x04, 0x08, 0x0D, 0x0E, 0x0F, 0x45)
RTHROTTLED = 0x58
GSM_SAFE = set(string.ascii_letters + string.digits + " \r\n!\"#%&'()*+,-./:;<=>?")
DELIVERY_TIMEOUT = 30  # seconds to wait for deliver_sm_resp


def cstr(data, pos):
    end = data.index(b"\0", pos)
    return data[pos:end].decode("latin-1"), end + 1


def pdu(command_id, status, seq, body=b""):
    return struct.pack(">IIII", 16 + len(body), command_id, status, seq) + body


def decode_text(raw, data_coding, esm_class):
    if esm_class & 0x40 and raw:  # strip user data header (concatenated SMS)
        raw = raw[raw[0] + 1:]
    if data_coding == 8:
        return raw.decode("utf-16-be", "replace")
    return raw.decode("latin-1")


def encode_text(text):
    if all(c in GSM_SAFE for c in text):
        return 0, text.encode("ascii")
    return 8, text.encode("utf-16-be")


def parse_submit(body):
    pos = 0
    _service, pos = cstr(body, pos)
    src_ton, src_npi = body[pos], body[pos + 1]
    src, pos = cstr(body, pos + 2)
    dst_ton, dst_npi = body[pos], body[pos + 1]
    dst, pos = cstr(body, pos + 2)
    esm_class, _proto, _prio = body[pos], body[pos + 1], body[pos + 2]
    _sched, pos = cstr(body, pos + 3)
    _valid, pos = cstr(body, pos)
    registered, _replace, coding, _default_id, sm_len = body[pos:pos + 5]
    pos += 5
    sm = body[pos:pos + sm_len]
    pos += sm_len
    while pos + 4 <= len(body):  # optional TLVs: use message_payload when short_message is empty
        tag, length = struct.unpack(">HH", body[pos:pos + 4])
        if tag == 0x0424 and not sm:
            sm = body[pos + 4:pos + 4 + length]
        pos += 4 + length
    return dict(source=src, destination=dst, body=decode_text(sm, coding, esm_class), source_ton=src_ton,
                source_npi=src_npi, dest_ton=dst_ton, dest_npi=dst_npi, registered_delivery=bool(registered & 1))


def build_deliver(source, destination, text):
    coding, payload = encode_text(text)
    src_ton, src_npi = (1, 1) if source.lstrip("+").isdigit() else (5, 0)
    head = (b"\0" + bytes([src_ton, src_npi]) + source.lstrip("+").encode("latin-1", "replace")[:20] + b"\0"
            + bytes([1, 1]) + destination.encode()[:20] + b"\0" + bytes([0, 0, 0]) + b"\0\0"
            + bytes([0, 0, coding, 0]))
    if len(payload) <= 254:
        return head + bytes([len(payload)]) + payload
    return head + b"\0" + struct.pack(">HH", 0x0424, len(payload)) + payload


# ---- database helpers (run in a worker thread) ----

def _retry(fn):
    """Retry a DB write briefly if the database is momentarily locked."""
    import functools
    import time as _t

    from django.db import OperationalError

    @functools.wraps(fn)
    def wrapper(*a, **k):
        for attempt in range(5):
            try:
                return fn(*a, **k)
            except OperationalError:
                if attempt == 4:
                    raise
                _t.sleep(0.05 * (attempt + 1))
    return wrapper


@sync_to_async
def authenticate(system_id, password, ip):
    from crm.models import SmppUser
    u = SmppUser.objects.select_related("account").filter(username=system_id).first()
    if not u or not u.enabled:
        return None, RINVSYSID
    if u.password != password:
        return None, RINVPASWD
    allowed = [x.strip() for x in u.ip.split(",") if x.strip()]
    if allowed and ip not in allowed:
        return None, RBINDFAIL
    return u, ROK


@sync_to_async
@_retry
def set_bound(user_id, bound):
    from crm.models import SmppUser
    SmppUser.objects.filter(pk=user_id).update(bound=bound, last_status_at=timezone.now())


@sync_to_async
def store_submit(username, fields):
    from .routing import receive
    return str(receive(smpp_username=username, **fields).uuid)


@sync_to_async
def wholesale_submit(user_id, body):
    """Returns (command_status, our message id or "")."""
    from crm.models import SmppUser
    from wholesale import engine, pdu as wpdu
    u = SmppUser.objects.select_related("account", "wholesale").get(pk=user_id)
    try:
        fields = wpdu.parse_submit_raw(body)
    except (ValueError, IndexError):
        return RSYSERR, ""
    res = engine.accept(u.account, fields, smpp_user=u)
    if res.status == ROK and res.message.test:
        engine.simulate_test(res.message)
    return res.status, (res.message.message_id if res.status == ROK else "")


@sync_to_async
def wholesale_settings(user_id):
    from wholesale.models import SmppTestSettings
    return SmppTestSettings.objects.get_or_create(smpp_user_id=user_id)[0].tps


@sync_to_async
@_retry
def wholesale_dlrs(account_id):
    from wholesale import engine, pdu as wpdu
    return [(m.pk, wpdu.build_receipt(m.source, m.destination, m.message_id, m.stat, m.err, m.created_at,
                                      m.done_at, m.body)) for m in engine.due_dlrs(account_id)]


@sync_to_async
@_retry
def wholesale_dlr_result(msg_pk, ok, error=""):
    from wholesale import engine
    engine.dlr_result(msg_pk, ok, error)


@sync_to_async
@_retry
def due_for(user_id, exclude):
    from .models import Message
    now = timezone.now()
    qs = (Message.objects.filter(client_bind_id=user_id, http_endpoint__isnull=True,
                                 forward_attempts__lt=settings.FORWARD_MAX_ATTEMPTS)
          .filter(Q(status=Message.RECEIVED) | Q(status=Message.FAILED)).exclude(pk__in=exclude)
          .order_by("received_at")[:50])
    out = []
    for m in qs:
        last = m.last_attempt_at or m.received_at
        if m.status == Message.FAILED and last + timedelta(minutes=2 ** m.forward_attempts) > now:
            continue
        m.forward_attempts += 1
        m.last_attempt_at = now
        m.save(update_fields=["forward_attempts", "last_attempt_at"])
        out.append((m.pk, m.source, m.destination, m.body))
    return out


@sync_to_async
@_retry
def delivery_result(msg_id, ok, error=""):
    from .models import Message
    fields = {"status": Message.FORWARDED if ok else Message.FAILED, "last_error": error[:300]}
    if ok:
        fields["forwarded_at"] = timezone.now()
    Message.objects.filter(pk=msg_id).update(**fields)


class Session:
    def __init__(self, server, reader, writer):
        self.server, self.reader, self.writer = server, reader, writer
        self.user, self.mode, self.seq = None, None, 0
        self.inflight = {}  # seq -> (message id, sent at)
        self.dlr_inflight = {}  # seq -> (wholesale message pk, sent at)
        self.tps = 0
        self.ip = (writer.get_extra_info("peername") or ("?",))[0]

    def next_seq(self):
        self.seq = self.seq % 0x7FFFFFFF + 1
        return self.seq

    async def send(self, data):
        self.writer.write(data)
        await self.writer.drain()

    @property
    def can_receive(self):
        return self.mode in (BIND_RX, BIND_TRX)

    async def run(self):
        try:
            while True:
                head = await self.reader.readexactly(16)
                length, cmd, status, seq = struct.unpack(">IIII", head)
                if length < 16 or length > 64 * 1024:
                    break
                body = await self.reader.readexactly(length - 16) if length > 16 else b""
                if not await self.handle(cmd, status, seq, body):
                    break
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        except Exception:
            logger.exception("SMPP session error from %s", self.ip)
        finally:
            await self.close()

    async def handle(self, cmd, status, seq, body):
        if cmd in (BIND_RX, BIND_TX, BIND_TRX):
            return await self.bind(cmd, seq, body)
        if cmd == ENQUIRE_LINK:
            await self.send(pdu(ENQUIRE_LINK | RESP, ROK, seq))
            return True
        if cmd == UNBIND:
            await self.send(pdu(UNBIND | RESP, ROK, seq))
            return False
        if cmd == DELIVER_SM | RESP and seq in self.dlr_inflight:
            mid, _ = self.dlr_inflight.pop(seq)
            await wholesale_dlr_result(mid, status == ROK, "" if status == ROK else f"SMPP error 0x{status:02X}")
            return True
        if cmd == DELIVER_SM | RESP:
            msg = self.inflight.pop(seq, None)
            if msg:
                await delivery_result(msg[0], status == ROK, "" if status == ROK else f"SMPP error 0x{status:02X}")
            return True
        if cmd & RESP:
            return True  # e.g. enquire_link_resp
        if not self.user:
            await self.send(pdu(cmd | RESP, RINVBNDSTS, seq))
            return True
        if cmd == SUBMIT_SM:
            return await self.submit(seq, body)
        await self.send(pdu(GENERIC_NACK, RINVCMDID, seq))
        return True

    async def bind(self, cmd, seq, body):
        try:
            system_id, pos = cstr(body, 0)
            password, pos = cstr(body, pos)
        except ValueError:
            await self.send(pdu(cmd | RESP, RBINDFAIL, seq))
            return False
        if self.user:
            await self.send(pdu(cmd | RESP, RALYBND, seq))
            return True
        user, err = await authenticate(system_id, password, self.ip)
        if user and user.role == user.CLIENT and cmd == BIND_TX:
            user, err = None, RBINDFAIL  # a client that can't receive is no use
        if not user:
            logger.warning("Bind refused for %r from %s (0x%02X)", system_id, self.ip, err)
            await self.send(pdu(cmd | RESP, err, seq))
            return False
        self.user, self.mode = user, cmd
        if user.role == user.WHOLESALE:
            self.tps = await wholesale_settings(user.pk)
        self.server.sessions.add(self)
        await set_bound(user.pk, True)
        logger.info("%s bound as %s from %s", user.username, user.role, self.ip)
        await self.send(pdu(cmd | RESP, ROK, seq, b"switch\0"))
        return True

    def throttled(self):
        """Token bucket per SMPP user, shared by all of that user's sessions."""
        if not self.tps:
            return False
        now = asyncio.get_running_loop().time()
        tokens, last = self.server.buckets.get(self.user.pk, (float(self.tps), now))
        tokens = min(float(self.tps), tokens + (now - last) * self.tps)
        if tokens < 1:
            self.server.buckets[self.user.pk] = (tokens, now)
            return True
        self.server.buckets[self.user.pk] = (tokens - 1, now)
        return False

    async def submit(self, seq, body):
        if self.user.role == self.user.WHOLESALE and self.mode != BIND_RX:
            if self.throttled():
                await self.send(pdu(SUBMIT_SM | RESP, RTHROTTLED, seq, b"\0"))
                return True
            try:
                status, msg_id = await wholesale_submit(self.user.pk, body)
            except Exception:
                logger.exception("wholesale submit_sm from %s failed", self.user.username)
                status, msg_id = RSYSERR, ""
            await self.send(pdu(SUBMIT_SM | RESP, status, seq, msg_id.encode() + b"\0"))
            return True
        if self.user.role != self.user.SUPPLIER or self.mode == BIND_RX:
            await self.send(pdu(SUBMIT_SM | RESP, RSUBMITFAIL, seq, b"\0"))
            return True
        try:
            msg_id = await store_submit(self.user.username, parse_submit(body))
        except Exception:
            logger.exception("submit_sm from %s failed", self.user.username)
            await self.send(pdu(SUBMIT_SM | RESP, RSYSERR, seq, b"\0"))
            return True
        await self.send(pdu(SUBMIT_SM | RESP, ROK, seq, msg_id.encode() + b"\0"))
        return True

    async def deliver_pending(self):
        now = asyncio.get_running_loop().time()
        for s, (mid, sent) in list(self.inflight.items()):
            if now - sent > DELIVERY_TIMEOUT:
                self.inflight.pop(s)
                await delivery_result(mid, False, "No deliver_sm_resp from client")
        for s, (mid, sent) in list(self.dlr_inflight.items()):
            if now - sent > DELIVERY_TIMEOUT:
                self.dlr_inflight.pop(s)
                await wholesale_dlr_result(mid, False, "No deliver_sm_resp from customer")
        if self.user and self.user.role == self.user.WHOLESALE and self.can_receive:
            for mid, body in await wholesale_dlrs(self.user.account_id):
                seq = self.next_seq()
                self.dlr_inflight[seq] = (mid, now)
                await self.send(pdu(DELIVER_SM, ROK, seq, body))
            return
        if not (self.user and self.user.role == self.user.CLIENT and self.can_receive):
            return
        for mid, src, dst, text in await due_for(self.user.pk, [m for m, _ in self.inflight.values()]):
            seq = self.next_seq()
            self.inflight[seq] = (mid, now)
            await self.send(pdu(DELIVER_SM, ROK, seq, build_deliver(src, dst, text)))

    async def close(self):
        self.server.sessions.discard(self)
        for mid, _ in self.inflight.values():
            await delivery_result(mid, False, "Client disconnected before confirming")
        self.inflight.clear()
        for mid, _ in self.dlr_inflight.values():
            await wholesale_dlr_result(mid, False, "Customer disconnected before confirming")
        self.dlr_inflight.clear()
        if self.user and not any(s.user and s.user.pk == self.user.pk for s in self.server.sessions):
            await set_bound(self.user.pk, False)
        try:
            self.writer.close()
        except Exception:
            pass


class SmppServer:
    def __init__(self, host="0.0.0.0", port=2775, poll=1.0):
        self.host, self.port, self.poll = host, port, poll
        self.sessions = set()
        self.buckets = {}
        self.server = None

    async def start(self):
        self.server = await asyncio.start_server(self._accept, self.host, self.port)
        self.port = self.server.sockets[0].getsockname()[1]
        self.task = asyncio.get_running_loop().create_task(self._deliver_loop())
        logger.info("SMPP server listening on %s:%s", self.host, self.port)

    async def stop(self):
        self.task.cancel()
        self.server.close()
        for s in list(self.sessions):
            s.writer.close()

    async def _accept(self, reader, writer):
        await Session(self, reader, writer).run()

    async def _deliver_loop(self):
        while True:
            for s in list(self.sessions):
                try:
                    await s.deliver_pending()
                except Exception:
                    logger.exception("Delivery to %s failed", s.user)
            await asyncio.sleep(self.poll)
