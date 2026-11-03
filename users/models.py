from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models.functions import Lower


class Role(models.TextChoices):
    ADMIN = "admin", "Administrator"
    DOCTOR = "doctor", "Shifokor"
    PATIENT = "patient", "Bemor"
    LAB = "lab", "Laborant"


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
    def is_lab(self):
        return self.role == Role.LAB

    @property
    def is_patient(self):
        return self.role == Role.PATIENT

    @property
    def is_locked(self):
        from django.utils import timezone
        return bool(self.locked_until and self.locked_until > timezone.now())

class LoginSession(models.Model):
    """Bitta kirish = bitta refresh token (jti). Foydalanuvchi qaysi qurilmalardan
    kirganini ko'radi va istalgan sessiyani yopa oladi."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="login_sessions")
    jti = models.CharField(max_length=255, unique=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField()
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-last_seen"]
        indexes = [models.Index(fields=["user", "revoked_at"])]

    def __str__(self):
        return f"{self.user} {self.ip} ({'yopilgan' if self.revoked_at else 'faol'})"
