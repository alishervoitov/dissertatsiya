from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import SecurityAlert


@receiver(post_save, sender=SecurityAlert, dispatch_uid="monitoring_alert_telegram")
def alert_to_telegram(sender, instance, created, **kwargs):
    # Faqat yangi ogohlantirish; hisoblagich oshishi (update) qayta yubormaydi.
    # Audit buzilishi kunlik langar vazifasida alohida 🚨 xabar bilan keladi.
    if created and instance.rule != SecurityAlert.AUDIT_TAMPER:
        from .notify import alert_message, notify

        notify(alert_message(instance))
