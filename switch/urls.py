from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import path

from core import views as core
from crm import views as crm
from inventory import views as inv
from sms import views as sms
from voice import views as voice

urlpatterns = [
    path("", core.dashboard, name="dashboard"),
    path("ui/", core.set_ui, name="set_ui"),
    path("health/", core.health),
    path("login/", auth_views.LoginView.as_view(template_name="core/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    # CRM
    path("accounts/", crm.account_list, name="account_list"),
    path("accounts/new/", crm.account_edit, name="account_new"),
    path("accounts/<int:pk>/", crm.account_detail, name="account_detail"),
    path("accounts/<int:pk>/edit/", crm.account_edit, name="account_edit"),
    path("accounts/<int:pk>/<slug:kind>/new/", crm.panel_edit, name="panel_new"),
    path("accounts/<int:pk>/<slug:kind>/<int:item>/", crm.panel_edit, name="panel_edit"),
    path("accounts/<int:pk>/<slug:kind>/<int:item>/delete/", crm.panel_delete, name="panel_delete"),
    path("binds/", crm.bind_status, name="bind_status"),
    # Numbers
    path("billing-groups/", inv.group_list, name="group_list"),
    path("billing-groups/new/", inv.group_edit, name="group_new"),
    path("billing-groups/<int:pk>/", inv.group_detail, name="group_detail"),
    path("billing-groups/<int:pk>/edit/", inv.group_edit, name="group_edit"),
    path("numbers/", inv.number_list, name="number_list"),
    path("numbers/add/", inv.add_numbers, name="add_numbers"),
    path("numbers/bulk/", inv.bulk_action, name="number_bulk"),
    # SMS
    path("sms/records/", sms.records, name="sms_records"),
    path("sms/ticker/", sms.ticker, name="sms_ticker"),
    path("sms/stats/", sms.stats, name="sms_stats"),
    path("sms/blocking/", sms.block_rules, name="block_rules"),
    path("sms/<int:pk>/resend/", sms.resend, name="sms_resend"),
    path("ingest/jasmin/", sms.ingest_jasmin, name="ingest_jasmin"),
    # Voice
    path("voice/cdrs/", voice.cdrs, name="cdrs"),
    path("voice/stats/", voice.stats, name="voice_stats"),
    path("ingest/freeswitch/", voice.ingest_freeswitch, name="ingest_freeswitch"),
    path("admin/", admin.site.urls),
]
