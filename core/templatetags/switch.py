from django import template

register = template.Library()


@register.simple_tag(takes_context=True)
def qs_without(context, *keys):
    q = context["request"].GET.copy()
    for k in keys or ("page",):
        q.pop(k, None)
    return q.urlencode()


@register.filter
def attr(obj, name):
    v = getattr(obj, name, "")
    return "" if v is None else v


@register.filter
def money(v):
    try:
        return f"{float(v or 0):,.2f}"
    except (TypeError, ValueError):
        return v


@register.filter
def e164(n):
    return "+" + n if n and n.isdigit() else n


@register.filter
def free(group):
    return (getattr(group, "n_total", 0) or 0) - (getattr(group, "n_alloc", 0) or 0)


@register.filter
def alloc_summary(allocs):
    """Collapse SMS + voice allocations to the same client into one line."""
    by_client = {}
    for a in allocs:
        by_client.setdefault(a.client_id, {"client": a.client, "channels": []})["channels"].append(a.channel)
    out = []
    for v in by_client.values():
        ch = v["channels"]
        v["label"] = "SMS & voice" if len(ch) == 2 else ("SMS" if ch[0] == "sms" else "Voice")
        out.append(v)
    return out


@register.filter
def get_item(d, key):
    return d.get(key) if hasattr(d, "get") else None
