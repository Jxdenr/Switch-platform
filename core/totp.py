"""Authenticator-app codes (TOTP, RFC 6238: SHA-1, 30 seconds, 6 digits), as used by Google
Authenticator, Microsoft Authenticator, 1Password, Authy and others."""
import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

STEP, DIGITS, DRIFT = 30, 6, 1  # accept the previous and next code too (clock drift)


def new_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _code(secret, step):
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** DIGITS).zfill(DIGITS)


def now_step(at=None):
    return int((time.time() if at is None else at) // STEP)


def verify(secret, code, last_step=0, at=None):
    """The time step the code matched, or None. A step at or before last_step is refused,
    so a code can't be replayed."""
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if not secret or len(code) != DIGITS:
        return None
    current = now_step(at)
    for step in range(current - DRIFT, current + DRIFT + 1):
        if step > last_step and hmac.compare_digest(_code(secret, step), code):
            return step
    return None


def uri(secret, account, issuer):
    label = quote(f"{issuer}:{account}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30"


def qr_svg(text):
    """Inline SVG QR code for the setup page (no image files, no third-party service)."""
    import qrcode
    import qrcode.image.svg
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    svg = img.to_string(encoding="unicode")
    return svg[svg.index("<svg"):]
