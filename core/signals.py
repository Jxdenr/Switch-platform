from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Profile


@receiver(post_save, sender=User)
def ensure_profile(sender, instance, created, **kwargs):
    # A new login starts with no portal and no areas: it can't see anything until it is given
    # a portal and a role (the Team and Platform pages do both).
    # Created inside a portal (e.g. by its owner)? It belongs to that portal.
    if created:
        from .tenancy import current_id
        Profile.objects.get_or_create(user=instance, defaults={
            "tenant_id": None if instance.is_superuser else current_id()})
