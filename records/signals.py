# records/signals.py
from django.db.models.signals import post_save
from django.dispatch import receiver

from users.models import Role, User
from .models import Patient


@receiver(post_save, sender=User)
def create_patient_profile(sender, instance, created, **kwargs):
    """Yangi 'bemor' roli bilan ro'yxatdan o'tgan har bir foydalanuvchi uchun
    avtomatik ravishda bo'sh Patient profili yaratiladi."""
    if created and instance.role == Role.PATIENT:
        Patient.objects.get_or_create(user=instance)