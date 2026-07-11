from django.contrib.auth.models import AbstractUser
from django.db import models


class Role(models.TextChoices):
    ADMIN = "admin", "Administrator"
    DOCTOR = "doctor", "Shifokor"
    PATIENT = "patient", "Bemor"


class User(AbstractUser):
    """
    Standart Django foydalanuvchisiga rol maydoni qo'shilgan.
    Parollar Django tomonidan avtomatik ravishda Argon2 bilan hash qilinadi
    (settings.py dagi PASSWORD_HASHERS ga qarang) - hech qachon ochiq matnda saqlanmaydi.
    """

    role = models.CharField(max_length=10, choices=Role.choices, default=Role.PATIENT)
    phone = models.CharField(max_length=20, blank=True)
    is_active_2fa = models.BooleanField(
        default=False, help_text="Kelajakda ikki bosqichli autentifikatsiya uchun"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.get_full_name() or self.username} ({self.get_role_display()})"

    @property
    def is_admin(self):
        return self.role == Role.ADMIN

    @property
    def is_doctor(self):
        return self.role == Role.DOCTOR

    @property
    def is_patient(self):
        return self.role == Role.PATIENT