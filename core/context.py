from django.conf import settings


def ui(request):
    """Colourway and layout for the shell; stored per user, overridable by cookie."""
    cw, layout = "harbour", "sidebar"
    user = getattr(request, "user", None)
    if user and user.is_authenticated and hasattr(user, "profile"):
        cw, layout = user.profile.colourway, user.profile.layout
    return {
        "ui_colourway": request.COOKIES.get("cw", cw),
        "ui_layout": request.COOKIES.get("layout", layout),
        "ui_debug": settings.DEBUG,
    }


ICONS = {
    "home": '<path d="M2 7.5 8 2.5l6 5V14H2z"/>',
    "acc": '<circle cx="8" cy="5.5" r="2.8"/><path d="M2.5 14c.8-3 3-4.3 5.5-4.3s4.7 1.3 5.5 4.3"/>',
    "grp": '<rect x="2" y="2.5" width="12" height="4" rx="1"/><rect x="2" y="9.5" width="12" height="4" rx="1"/>',
    "num": '<path d="M5.5 2 4 14M12 2l-1.5 12M2 5.5h12M1.5 10.5h12"/>',
    "add": '<path d="M8 3v10M3 8h10"/>',
    "msg": '<path d="M2 3h12v8H6l-4 3z"/>',
    "live": '<circle cx="8" cy="8" r="2"/><path d="M4.5 4.5a5 5 0 0 0 0 7M11.5 4.5a5 5 0 0 1 0 7"/>',
    "stat": '<path d="M2.5 13.5V8M6.5 13.5V3M10.5 13.5V6.5M14 13.5V10"/>',
    "bind": '<path d="M6 10 10 6M4.5 7.5 3 9a2.5 2.5 0 0 0 3.5 3.5L8 11M8 5l1.5-1.5A2.5 2.5 0 0 1 13 7l-1.5 1.5"/>',
    "block": '<circle cx="8" cy="8" r="5.8"/><path d="m4 4 8 8"/>',
    "phone": '<path d="M4 2h2.5l1 3-1.5 1a7 7 0 0 0 3.5 3.5l1-1.5 3 1V12a2 2 0 0 1-2 2A10 10 0 0 1 2 4a2 2 0 0 1 2-2z"/>',
    "set": '<circle cx="8" cy="8" r="2.2"/><path d="M8 1.5v2M8 12.5v2M1.5 8h2M12.5 8h2M3.4 3.4l1.4 1.4M11.2 11.2l1.4 1.4M3.4 12.6l1.4-1.4M11.2 4.8l1.4-1.4"/>',
}

NAV = [
    ("Overview", [("dashboard", "Dashboard", "/", "home", False)]),
    ("Accounts", [("accounts", "Suppliers & clients", "/accounts/", "acc", True),
                  ("binds", "Bind status", "/binds/", "bind", True)]),
    ("Numbers", [("groups", "Billing groups", "/billing-groups/", "grp", True),
                 ("numbers", "Numbers", "/numbers/", "num", False),
                 ("addnumbers", "Add numbers", "/numbers/add/", "add", True)]),
    ("SMS", [("records", "SMS records", "/sms/records/", "msg", False),
             ("ticker", "Live ticker", "/sms/ticker/", "live", False),
             ("stats", "SMS stats", "/sms/stats/", "stat", True),
             ("blocking", "Blocking rules", "/sms/blocking/", "block", True)]),
    ("Voice", [("cdrs", "Call records", "/voice/cdrs/", "phone", False),
               ("voicestats", "Voice stats", "/voice/stats/", "stat", True)]),
    ("Admin", [("admin", "Users & settings", "/admin/", "set", True)]),
]


def nav(request):
    from django.utils.safestring import mark_safe
    from .access import client_account
    user = getattr(request, "user", None)
    is_client = bool(user and user.is_authenticated and client_account(user))
    groups = []
    for title, items in NAV:
        visible = [{"key": k, "label": lbl, "url": url,
                    "icon": mark_safe(f'<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" '
                                      f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{ICONS[ic]}</svg>')}
                   for k, lbl, url, ic, staff_only in items if not (staff_only and is_client)]
        if visible:
            groups.append({"title": title, "items": visible})
    binds_down = 0
    if user and user.is_authenticated and not is_client:
        from crm.models import SmppUser, Smsc
        binds_down = (SmppUser.objects.filter(enabled=True, bound=False).count()
                      + Smsc.objects.filter(enabled=True, bound=False).count())
    return {"nav_groups": groups, "binds_down": binds_down, "is_client_user": is_client}
