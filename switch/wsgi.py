"""
WSGI config for switch project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.1/howto/deployment/wsgi/
"""

import os

from django.core.wsgi import get_wsgi_application

# Web requests are only ever scoped to one portal (see core.tenancy).
os.environ.pop("SWITCH_CLI_ALL_PORTALS", None)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "switch.settings")

application = get_wsgi_application()
