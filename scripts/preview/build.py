# Builds the static clickable preview (fake demo data) by crawling the admin UI with Django's test client.
# Usage (fresh sqlite DB): migrate, seed_lookups, demo_data, createsuperuser admin, then
#   ALLOWED_HOSTS=testserver python scripts/preview/build.py OUT_DIR scripts/preview/banner.html
import os, sys, re, hashlib, shutil
from urllib.parse import urlsplit, unquote
sys.path.insert(0, os.getcwd())
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "switch.settings")
import django; django.setup()
from django.test import Client
from django.contrib.auth.models import User

OUT = sys.argv[1]
os.makedirs(OUT, exist_ok=True)
c = Client(); c.force_login(User.objects.get(username="admin"))
SKIP = re.compile(r"^/(logout|ui|admin|health|ingest|api|login)/|/delete/|/send/|/bulk/|export|\.csv|download|resend")
BANNER = open(sys.argv[2]).read()

def fname(url):
    u = urlsplit(url); p = u.path.strip("/")
    if not p and not u.query: return "index.html"
    base = p.replace("/", "-") or "home"
    if u.query: base += "-" + hashlib.md5(u.query.encode()).hexdigest()[:6]
    return base + ".html"

seen, queue, pages = set(), ["/"], {}
MAX = 420
while queue and len(pages) < MAX:
    url = queue.pop(0)
    if url in seen: continue
    seen.add(url)
    r = c.get(url)
    if r.status_code != 200 or "text/html" not in r.get("Content-Type", ""): continue
    html = r.content.decode()
    pages[url] = html
    for h in re.findall(r'href="([^"#]*)"', html):
        h = h.replace("&amp;", "&")
        if not h or h.startswith(("http", "mailto:", "javascript:", "/static/")): continue
        if h.startswith("?"): h = urlsplit(url).path + h
        if not h.startswith("/"): continue
        if SKIP.search(h): continue
        if h not in seen: queue.append(h)

names = {u: fname(u) for u in pages}
def rewrite(html, cur):
    def rep(m):
        h = m.group(1).replace("&amp;", "&")
        if h.startswith("/static/css/app.css"): return 'href="app.css"'
        if h.startswith("?"): h = urlsplit(cur).path + h
        if h.startswith("#") or h.startswith(("http", "mailto:")): return m.group(0)
        if h in names: return f'href="{names[h]}"'
        return 'href="#" data-off="1"'
    html = re.sub(r'href="([^"]*)"', rep, html)
    html = re.sub(r'action="[^"]*"', 'action="#"', html)
    return html.replace("</body>", BANNER + "</body>")
for u, h in pages.items():
    open(os.path.join(OUT, names[u]), "w").write(rewrite(h, u))
shutil.copy("static/css/app.css", os.path.join(OUT, "app.css"))
print(len(pages), "pages; queue left", len(queue))
