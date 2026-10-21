from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models.functions import Lower


class Role(models.TextChoices):
    ADMIN = "admin", "Administrator"
    DOCTOR = "doctor", "Shifokor"
    PATIENT = "patient", "Bemor"


class User(AbstractUser):

    role = models.CharField(max_length=10, choices=Role.choices, default=Role.PATIENT)
    phone = models.CharField(max_length=20, blank=True)
    avatar = models.ImageField(upload_to="avatars/", blank=True, null=True)
    failed_login_attempts = models.PositiveIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)
    is_active_2fa = models.BooleanField(
        default=False, help_text="Kelajakda ikki bosqichli autentifikatsiya uchun"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta(AbstractUser.Meta):
        constraints = [
            # 'Alisher' va 'alisher' ikki xil hisob bo'lmasin (taqlid xavfi). API validatorlaridan
            # tashqari admin sayti, shell va bir vaqtdagi so'rovlarni ham qamraydi.
            models.UniqueConstraint(
                Lower("username"),
                name="users_user_username_ci_uniq",
                violation_error_message="Bu foydalanuvchi nomi band, boshqasini tanlang.",
            ),
        ]

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

    @property
    def is_locked(self):
        from django.utils import timezone
        return bool(self.locked_until and self.locked_until > timezone.now())