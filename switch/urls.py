from django.conf import settings
from django.contrib import admin
from django.urls import path

from core import auth_views as auth
from core import team_views as team
from core import views as core
from crm import views as crm
from inventory import views as inv
from sms import views as sms
from voice import views as voice
from wholesale import api as wapi
from wholesale import views as ws

# Django admin's own login would skip lockout and two-factor: use ours.
admin.site.login = lambda request, extra_context=None: auth.login_view(request)

urlpatterns = [
    path("", core.dashboard, name="dashboard"),
    path("ui/", core.set_ui, name="set_ui"),
    path("health/", core.health, name="health"),
    # Sign in and security
    path("login/", auth.login_view, name="login"),
    path("login/code/", auth.login_code, name="login_code"),
    path("logout/", auth.logout_view, name="logout"),
    path("password/forgot/", auth.password_reset, name="password_reset"),
    path("password/set/<uidb64>/<token>/", auth.set_password, name="set_password"),
    path("account/security/", auth.security_page, name="security"),
    # Logins and portals
    path("team/", team.team, name="team"),
    path("team/new/", team.team_edit, name="team_new"),
    path("team/<int:pk>/", team.team_edit, name="team_edit"),
    path("team/<int:pk>/action/", team.team_action, name="team_action"),
    path("settings/", team.portal_settings, name="portal_settings"),
    path("platform/", team.platform, name="platform"),
    path("platform/<int:pk>/", team.platform_portal, name="platform_portal"),
    path("platform/<int:pk>/owners/<int:profile_pk>/", team.platform_owner_action, name="platform_owner_action"),
    # CRM
    path("accounts/", crm.account_list, name="account_list"),
    path("suppliers/", crm.account_list, {"kind": "supplier"}, name="supplier_list"),
    path("clients/", crm.account_list, {"kind": "client"}, name="client_list"),
    path("accounts/<int:pk>/documents/<slug:kind>/<str:month>/", crm.document_view, name="document"),
    path("accounts/<int:pk>/documents/send/", crm.document_send, name="document_send"),
    path("accounts/new/", crm.account_edit, name="account_new"),
    path("accounts/<int:pk>/", crm.account_detail, name="account_detail"),
    path("accounts/<int:pk>/edit/", crm.account_edit, name="account_edit"),
    path("accounts/<int:pk>/<slug:kind>/new/", crm.panel_edit, name="panel_new"),
    path("accounts/<int:pk>/<slug:kind>/<int:item>/", crm.panel_edit, name="panel_edit"),
    path("accounts/<int:pk>/<slug:kind>/<int:item>/delete/", crm.panel_delete, name="panel_delete"),
    path("binds/", crm.bind_status, name="bind_status"),
    path("statements/", crm.client_statements, name="client_statements"),
    # Numbers
    path("billing-groups/", inv.group_list, name="group_list"),
    path("billing-groups/new/", inv.group_edit, name="group_new"),
    path("billing-groups/<int:pk>/", inv.group_detail, name="group_detail"),
    path("billing-groups/<int:pk>/edit/", inv.group_edit, name="group_edit"),
    path("numbers/", inv.number_list, name="number_list"),
    path("numbers/add/", inv.add_numbers, name="add_numbers"),
    path("numbers/bulk/", inv.bulk_action, name="number_bulk"),
    path("numbers/allocate/", inv.allocate_quantity, name="allocate_quantity"),
    # SMS
    path("sms/records/", sms.records, name="sms_records"),
    path("sms/ticker/", sms.ticker, name="sms_ticker"),
    path("sms/stats/", sms.stats, name="sms_stats"),
    path("sms/blocking/", sms.block_rules, name="block_rules"),
    path("sms/<int:pk>/resend/", sms.resend, name="sms_resend"),
    path("ingest/jasmin/", sms.ingest_jasmin, name="ingest_jasmin"),
    path("api/sms/inbound/", sms.api_inbound, name="api_inbound"),
    # Wholesale
    path("api/wholesale/send/", wapi.send, name="wholesale_send"),
    path("wholesale/", ws.overview, name="ws_overview"),
    path("wholesale/messages/", ws.message_list, name="ws_messages"),
    path("wholesale/quality/", ws.quality, name="ws_quality"),
    path("wholesale/routes/", ws.routes, name="ws_routes"),
    path("wholesale/networks/", ws.networks, name="ws_networks"),
    path("wholesale/accounts/<int:pk>/", ws.account, name="ws_account"),
    path("wholesale/smpp/<int:pk>/", ws.smpp_settings, name="ws_smpp"),
    # Voice
    path("voice/cdrs/", voice.cdrs, name="cdrs"),
    path("voice/stats/", voice.stats, name="voice_stats"),
    path("ingest/freeswitch/", voice.ingest_freeswitch, name="ingest_freeswitch"),
] + ([path("admin/", admin.site.urls)] if settings.DJANGO_ADMIN_ENABLED else [])
