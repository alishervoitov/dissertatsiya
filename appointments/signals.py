from django.db.models.signals import post_save
from django.dispatch import receiver

from users.models import Role, User

from .models import DoctorProfile


@receiver(post_save, sender=User)
def create_doctor_profile(sender, instance, **kwargs):
    """Shifokor roliga ega har bir foydalanuvchida qabul sozlamalari bo'lsin."""
    if instance.role == Role.DOCTOR:
        DoctorProfile.objects.get_or_create(user=instance)
