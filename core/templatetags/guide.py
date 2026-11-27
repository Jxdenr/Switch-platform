"""Help on every page: {% page_help %}, {% tip "margin" %}, {% empty_row 7 "suppliers" %}, {% setup_checklist %}."""
from django import template
from django.utils.html import format_html

from core import help as h

register = template.Library()


@register.inclusion_tag("partials/page_help.html", takes_context=True)
def page_help(context, key=None):
    key = key or context.get("nav")
    return {"key": key, "p": h.page(key, context.get("is_client_user"))}


@register.simple_tag
def tip(key_or_text):
    """A small (i) that explains a word. Pass a glossary key or plain text."""
    text = h.GLOSSARY.get(key_or_text, key_or_text)
    return format_html('<span class="tip" tabindex="0" role="img" aria-label="{}" data-tip="{}">i</span>', text, text)


@register.inclusion_tag("partials/empty_row.html", takes_context=True)
def empty_row(context, cols, key, title=None, text=None):
    t, x, cta, url = h.EMPTY.get(key, ("Nothing here yet", "", "", ""))
    if context.get("is_client_user"):
        cta = url = ""
    return {"cols": cols, "title": title or t, "text": text if text is not None else x, "cta": cta, "url": url}


@register.inclusion_tag("partials/setup_checklist.html")
def setup_checklist():
    tracks = h.setup_steps()
    for t in tracks:
        t["done"] = sum(1 for s in t["steps"] if s[1])
        t["total"] = len(t["steps"])
        t["next"] = next((s for s in t["steps"] if not s[1]), None)
    return {"tracks": tracks, "all_done": all(t["done"] == t["total"] for t in tracks)}
