"""HTTP API for wholesale customers who don't use SMPP.

POST /api/wholesale/send/  (header X-Api-Key, or key=)
  to, from, text            required
  Long text is sent as one message part per 153 GSM / 67 UCS-2 characters.
Answers {"status": "accepted", "ids": [...]} or {"status": "error", "error": "..."}.
Receipts are POSTed to the key's DLR callback URL: id, status, err, to, from, done_at, test.
"""
import random
import time

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from core.access import machine_endpoint
from core.security import client_ip as _client_ip
from sms.smpp_server import GSM_SAFE
from . import engine
from .models import ApiKey

_buckets = {}  # per process; with several web workers the real limit is workers x tps


def _throttled(key):
    now = time.monotonic()
    tokens, last = _buckets.get(key.pk, (float(key.tps), now))
    tokens = min(float(key.tps), tokens + (now - last) * key.tps)
    if tokens < 1:
        _buckets[key.pk] = (tokens, now)
        return True
    _buckets[key.pk] = (tokens - 1, now)
    return False


def split(text):
    """[(data_coding, esm_class, payload bytes, (ref, num, total) or None)]"""
    gsm = all(c in GSM_SAFE for c in text)
    coding, raw = (0, text.encode("ascii")) if gsm else (8, text.encode("utf-16-be"))
    single, part = (160, 153) if gsm else (140, 134)
    if len(raw) <= single:
        return [(coding, 0, raw, None)]
    step = part if gsm else part - part % 2
    chunks = [raw[i:i + step] for i in range(0, len(raw), step)]
    ref = random.randint(0, 255)
    return [(coding, 0x40, bytes([5, 0, 3, ref, len(chunks), n + 1]) + c, (ref, n + 1, len(chunks)))
            for n, c in enumerate(chunks)]


ERRORS = {engine.RINVDSTADR: 400, engine.RINVSRCADR: 400, engine.RSUBMITFAIL: 422}


@machine_endpoint
@csrf_exempt
@require_POST
def send(request):
    p = request.POST
    key = ApiKey.objects.select_related("account").filter(
        key=request.headers.get("X-Api-Key") or p.get("key", ""), enabled=True).first()
    if not key:
        return JsonResponse({"status": "error", "error": "invalid key"}, status=403)
    if not key.allows(_client_ip(request)):
        return JsonResponse({"status": "error", "error": "IP not allowed"}, status=403)
    to, frm, text = p.get("to", ""), p.get("from", ""), p.get("text") or p.get("message", "")
    if not to or not text:
        return JsonResponse({"status": "error", "error": "to and text are required"}, status=400)
    parts = split(text)
    if _throttled(key):
        return JsonResponse({"status": "error", "error": "throttled: over your messages per second"}, status=429)
    ids = []
    for coding, esm, payload, udh in parts:
        fields = dict(source=frm, destination=to, esm_class=esm, data_coding=coding, payload=payload,
                      body=text if not udh else payload[6:].decode("utf-16-be" if coding == 8 else "latin-1",
                                                                      "replace"),
                      part_ref=udh[0] if udh else None, part_num=udh[1] if udh else None,
                      part_total=udh[2] if udh else None)
        res = engine.accept(key.account, fields, api_key=key)
        if res.status != engine.ROK:
            return JsonResponse({"status": "error", "error": res.reason, "ids": ids},
                                status=402 if "credit" in res.reason else ERRORS.get(res.status, 422))
        if res.message.test:
            engine.simulate_test(res.message)
        ids.append(res.message.message_id)
    ApiKey.objects.filter(pk=key.pk).update(last_used=timezone.now())
    return JsonResponse({"status": "accepted", "ids": ids})
