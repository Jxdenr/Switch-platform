"""SMPP 3.4 pieces for wholesale: raw submit_sm parsing (so we can pass the message on to the
vendor byte for byte), submit_sm building, and delivery receipts in both directions.

Receipt text format (SMPP 3.4 Appendix B, used by nearly every SMSC):
  id:IIIIIIIIII sub:SSS dlvrd:DDD submit date:YYMMDDhhmm done date:YYMMDDhhmm stat:DDDDDDD err:E text:...
plus the TLVs receipted_message_id (0x001E), message_state (0x0427) and network_error_code (0x0423).
"""
import re
import struct
from datetime import datetime, timezone as dt_tz

from sms.smpp_server import cstr, decode_text

TLV_RECEIPTED_ID, TLV_MESSAGE_STATE, TLV_NETWORK_ERROR, TLV_PAYLOAD = 0x001E, 0x0427, 0x0423, 0x0424
ESM_RECEIPT, ESM_UDHI = 0x04, 0x40

# message_state values (SMPP 3.4 5.2.28) and the stat words in receipt text
STATE_TO_STAT = {1: "ENROUTE", 2: "DELIVRD", 3: "EXPIRED", 4: "DELETED", 5: "UNDELIV", 6: "ACCEPTD",
                 7: "UNKNOWN", 8: "REJECTD"}
STAT_TO_STATE = {v: k for k, v in STATE_TO_STAT.items()}
# Vendors spell stats in many ways; map them to the standard words without changing their meaning.
STAT_ALIASES = {"DELIVERED": "DELIVRD", "DELIVRD": "DELIVRD", "DELIVR": "DELIVRD",
                "UNDELIVERABLE": "UNDELIV", "UNDELIVERED": "UNDELIV", "UNDELIV": "UNDELIV", "FAILED": "UNDELIV",
                "EXPIRED": "EXPIRED", "EXPIRD": "EXPIRED", "DELETED": "DELETED",
                "REJECTED": "REJECTD", "REJECTD": "REJECTD", "ACCEPTED": "ACCEPTD", "ACCEPTD": "ACCEPTD",
                "ENROUTE": "ENROUTE", "UNKNOWN": "UNKNOWN"}
FINAL_STATS = {"DELIVRD", "UNDELIV", "EXPIRED", "DELETED", "REJECTD", "UNKNOWN"}


def parse_submit_raw(body):
    """Every submit_sm field we need, with the short_message kept as raw bytes."""
    pos = 0
    service_type, pos = cstr(body, pos)
    src_ton, src_npi = body[pos], body[pos + 1]
    src, pos = cstr(body, pos + 2)
    dst_ton, dst_npi = body[pos], body[pos + 1]
    dst, pos = cstr(body, pos + 2)
    esm_class, protocol_id, priority = body[pos], body[pos + 1], body[pos + 2]
    _sched, pos = cstr(body, pos + 3)
    validity, pos = cstr(body, pos)
    registered, _replace, coding, _default_id, sm_len = body[pos:pos + 5]
    pos += 5
    sm = bytes(body[pos:pos + sm_len])
    pos += sm_len
    while pos + 4 <= len(body):
        tag, length = struct.unpack(">HH", body[pos:pos + 4])
        if tag == TLV_PAYLOAD and not sm:
            sm = bytes(body[pos + 4:pos + 4 + length])
        pos += 4 + length
    ref = num = total = None
    if esm_class & ESM_UDHI and sm:
        ref, num, total = parse_udh(sm)
    return dict(source=src, source_ton=src_ton, source_npi=src_npi, destination=dst, dest_ton=dst_ton,
                esm_class=esm_class, data_coding=coding, payload=sm, registered_delivery=registered,
                body=decode_text(sm, coding, esm_class), part_ref=ref, part_num=num, part_total=total,
                validity=validity)


def parse_udh(sm):
    """Concatenation info from a user data header: (reference, part number, total parts)."""
    try:
        udhl, pos = sm[0], 1
        while pos < 1 + udhl:
            iei, iel = sm[pos], sm[pos + 1]
            data = sm[pos + 2:pos + 2 + iel]
            if iei == 0x00 and iel == 3:
                return data[0], data[2], data[1]
            if iei == 0x08 and iel == 4:
                return (data[0] << 8) | data[1], data[3], data[2]
            pos += 2 + iel
    except IndexError:
        pass
    return None, None, None


def submit_sm(source, source_ton, source_npi, destination, esm_class, data_coding, payload, registered=1):
    """Build a submit_sm body. The message bytes go through unchanged (UDH, coding and all)."""
    head = (b"\0" + bytes([source_ton, source_npi]) + source.encode("latin-1", "replace")[:20] + b"\0"
            + bytes([1, 1]) + destination.encode()[:20] + b"\0"
            + bytes([esm_class & ESM_UDHI, 0, 0]) + b"\0\0" + bytes([registered, 0, data_coding, 0]))
    if len(payload) <= 254:
        return head + bytes([len(payload)]) + payload
    return head + b"\0" + struct.pack(">HH", TLV_PAYLOAD, len(payload)) + payload


def auto_ton(source):
    """International number -> TON 1/NPI 1; short code -> 3/0; alphanumeric -> 5/0."""
    s = source.lstrip("+")
    if s.isdigit():
        return (1, 1) if len(s) > 8 else (3, 0)
    return 5, 0


# ---------- receipts ----------

DATE = r"(\d{10,12})"
RX = {k: re.compile(p, re.I) for k, p in {
    "id": r"\bid:\s*([^\s]+)", "sub": r"\bsub:\s*(\d+)", "dlvrd": r"\bdlvrd:\s*(\d+)",
    "submit": r"submit\s*date:\s*" + DATE, "done": r"done\s*date:\s*" + DATE,
    "stat": r"\bstat:\s*([A-Za-z_]+)", "err": r"\berr:\s*([0-9A-Za-z]+)"}.items()}


def normal_stat(raw):
    return STAT_ALIASES.get((raw or "").upper(), (raw or "").upper()[:7])


def parse_receipt(esm_class, text_bytes, tlvs):
    """A vendor's deliver_sm receipt -> dict(id, stat, err, network_error, done_at, raw).
    The TLVs win over the text when both are present (they are less often mangled)."""
    text = text_bytes.decode("latin-1", "replace") if isinstance(text_bytes, (bytes, bytearray)) else text_bytes
    found = {k: (m.group(1) if (m := rx.search(text)) else "") for k, rx in RX.items()}
    msg_id = tlvs.get(TLV_RECEIPTED_ID, b"").rstrip(b"\0").decode("latin-1") or found["id"]
    stat = normal_stat(found["stat"])
    if TLV_MESSAGE_STATE in tlvs and tlvs[TLV_MESSAGE_STATE]:
        stat = STATE_TO_STAT.get(tlvs[TLV_MESSAGE_STATE][0], stat)
    net_err = ""
    if len(tlvs.get(TLV_NETWORK_ERROR, b"")) == 3:
        t, code = tlvs[TLV_NETWORK_ERROR][0], struct.unpack(">H", tlvs[TLV_NETWORK_ERROR][1:])[0]
        net_err = f"{t}:{code}"
    return dict(id=msg_id.strip(), stat=stat, err=found["err"][:10], network_error=net_err,
                done_at=parse_date(found["done"]), raw=text[:300])


def parse_date(s):
    if not s:
        return None
    for fmt in ("%y%m%d%H%M%S", "%y%m%d%H%M"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=dt_tz.utc)
        except ValueError:
            continue
    return None


def is_receipt(esm_class, text_bytes, tlvs):
    return bool(esm_class & ESM_RECEIPT) or TLV_RECEIPTED_ID in tlvs or (
        isinstance(text_bytes, (bytes, bytearray)) and text_bytes[:3].lower() == b"id:")


def parse_deliver(body):
    """deliver_sm body -> (esm_class, short_message bytes, {tag: value})."""
    pos = 0
    _st, pos = cstr(body, pos)
    _src, pos = cstr(body, pos + 2)
    _dst, pos = cstr(body, pos + 2)
    esm_class = body[pos]
    _sched, pos = cstr(body, pos + 3)
    _valid, pos = cstr(body, pos)
    sm_len = body[pos + 4]
    pos += 5
    sm = bytes(body[pos:pos + sm_len])
    pos += sm_len
    tlvs = {}
    while pos + 4 <= len(body):
        tag, length = struct.unpack(">HH", body[pos:pos + 4])
        tlvs[tag] = bytes(body[pos + 4:pos + 4 + length])
        pos += 4 + length
    return esm_class, sm, tlvs


def receipt_text(message_id, stat, err, submit_at, done_at, body=""):
    dlvrd = "001" if stat == "DELIVRD" else "000"
    sd = submit_at.strftime("%y%m%d%H%M") if submit_at else "0000000000"
    dd = done_at.strftime("%y%m%d%H%M") if done_at else sd
    return (f"id:{message_id} sub:001 dlvrd:{dlvrd} submit date:{sd} done date:{dd} "
            f"stat:{stat} err:{(err or '000')[:3].rjust(3, '0')} text:{(body or '')[:20]}")


def build_receipt(source, destination, message_id, stat, err, submit_at, done_at, body=""):
    """deliver_sm body carrying a receipt to a customer. Addresses are swapped, as the spec says:
    the receipt comes FROM the handset number TO the original sender."""
    text = receipt_text(message_id, stat, err, submit_at, done_at, body).encode("latin-1", "replace")
    d_ton, d_npi = auto_ton(source)
    head = (b"\0" + bytes([1, 1]) + destination.encode()[:20] + b"\0"
            + bytes([d_ton, d_npi]) + source.encode("latin-1", "replace")[:20] + b"\0"
            + bytes([ESM_RECEIPT, 0, 0]) + b"\0\0" + bytes([0, 0, 0, 0]) + bytes([len(text)]) + text)
    mid = message_id.encode() + b"\0"
    head += struct.pack(">HH", TLV_RECEIPTED_ID, len(mid)) + mid
    head += struct.pack(">HHB", TLV_MESSAGE_STATE, 1, STAT_TO_STATE.get(stat, 7))
    return head


# ---------- vendor message ID formats ----------

HEX = re.compile(r"^[0-9a-fA-F]+$")


def id_alternative(raw):
    """Vendors often answer submit_sm_resp with one base and put the other in the receipt.
    Store the hex-read value as decimal so either form matches later."""
    raw = (raw or "").strip().lower()
    if raw and HEX.match(raw) and len(raw) <= 30:
        return str(int(raw, 16))
    return ""


def id_candidates(dlr_id):
    """Every spelling of a receipt ID that could be the ID from submit_sm_resp."""
    x = (dlr_id or "").strip().lower()
    out = {x, x.lstrip("0") or "0"}
    if x.isdigit() and len(x) <= 30:
        out.add(format(int(x), "x"))
    if x and HEX.match(x) and len(x) <= 30:
        out.add(str(int(x, 16)))
    return {o for o in out if o}
