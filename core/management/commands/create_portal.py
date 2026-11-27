"""Create a portal and its owner login from the server's command line (first deploy).

    manage.py create_portal "Konekta Ltd" you@example.com --name Jaden --site https://switch.example.com

Prints a one-time link for the owner to choose a password. Running it again for an existing
portal adds another owner (or prints a fresh link for an existing one).
"""
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.management.base import BaseCommand, CommandError
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from core.models import Profile, Tenant


class Command(BaseCommand):
    help = "Create a portal (if needed) and an owner login for it, and print their set-password link."

    def add_arguments(self, parser):
        parser.add_argument("portal")
        parser.add_argument("email")
        parser.add_argument("--name", default="", help="Owner's first name")
        parser.add_argument("--site", default="https://localhost:8000", help="Public address of the switch")

    def handle(self, portal, email, name, site, **opts):
        User = get_user_model()
        email = email.strip().lower()
        t, created = Tenant.objects.get_or_create(name=portal, defaults={"company_name": portal})
        u = User.objects.filter(email__iexact=email).first()
        if u and u.profile.tenant_id not in (None, t.pk):
            raise CommandError(f"{email} already has a login in another portal.")
        if u and u.is_superuser:
            raise CommandError(f"{email} is a platform admin. Use a different email for the portal owner.")
        if not u:
            u = User(username=email, email=email, first_name=name)
            u.set_unusable_password()
            u.save()
        Profile.objects.filter(user=u).update(tenant=t, role=Profile.ROLE_OWNER)
        link = site.rstrip("/") + reverse("set_password", args=[urlsafe_base64_encode(force_bytes(u.pk)),
                                                                default_token_generator.make_token(u)])
        self.stdout.write(f"{'Created' if created else 'Using'} portal \"{t}\". Owner: {email}")
        self.stdout.write(f"Set-password link (works once, 3 days):\n{link}")
