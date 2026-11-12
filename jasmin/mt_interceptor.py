"""Jasmin MT interceptor: hands SMS that suppliers submit over OUR SMPP server to the app.

Install with jCli:
    mtinterceptor -a
    type DefaultInterceptor
    script python3(/etc/jasmin/mt_interceptor.py)
    ok

Jasmin runs this for every submit_sm. We post the message to the app's ingest endpoint
and answer the supplier with success. Set SWITCH_INGEST_URL in the Jasmin container.
VERIFY on a test bind: interceptor globals differ between Jasmin versions.
"""
import os
import urllib.parse
import urllib.request

url = os.environ.get("SWITCH_INGEST_URL", "http://web:8000/ingest/jasmin/?token=dev-ingest-token")
pdu = routable.pdu  # noqa: F821  (provided by Jasmin)
username = routable.user.username  # noqa: F821

data = urllib.parse.urlencode({
    "from": pdu.params["source_addr"].decode() if isinstance(pdu.params["source_addr"], bytes) else pdu.params["source_addr"],
    "to": pdu.params["destination_addr"].decode() if isinstance(pdu.params["destination_addr"], bytes) else pdu.params["destination_addr"],
    "content": pdu.params["short_message"].decode("utf-8", "replace") if isinstance(pdu.params["short_message"], bytes) else pdu.params["short_message"],
    "source_addr_ton": int(pdu.params.get("source_addr_ton", 0) or 0),
    "dest_addr_ton": int(pdu.params.get("dest_addr_ton", 0) or 0),
}).encode()
sep = "&" if "?" in url else "?"
try:
    urllib.request.urlopen(url + sep + "smpp_user=" + urllib.parse.quote(username), data=data, timeout=5)
    smpp_status = 0  # ESME_ROK: accepted
except Exception:
    smpp_status = 8  # ESME_RSYSERR: supplier will retry
