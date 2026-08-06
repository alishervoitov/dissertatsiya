from django.conf import settings
from django.db import models

from .encryption import EncryptedCharField, EncryptedTextField


class Patient(models.Model):
    """Bemor profili. Foydalanuvchi hisobi bilan bir-birga bog'langan (1:1)."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="patient_profile"
    )
    date_of_birth = models.DateField(null=True, blank=True)
    GENDER_CHOICES = [("M", "Erkak"), ("F", "Ayol"), ("O", "Boshqa")]
    gender = models.CharField(max_length=1, choices=GENDER_CHOICES, blank=True)

    # Nozik shaxsiy ma'lumotlar - shifrlangan holda saqlanadi
    national_id = EncryptedCharField(blank=True, help_text="Shaxsni tasdiqlovchi hujjat raqami")
    address = EncryptedTextField(blank=True)
    emergency_contact = EncryptedCharField(blank=True)
    blood_type = models.CharField(max_length=5, blank=True)
    allergies = EncryptedTextField(blank=True, help_text="Ma'lum allergiyalar")

    # Bemorga biriktirilgan asosiy shifokor (ixtiyoriy)
    primary_doctor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_patients",
        limit_choices_to={"role": "doctor"},
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Bemor: {self.user.get_full_name() or self.user.username}"


class MedicalRecord(models.Model):
    """
    Bitta tashrif/tashxis yozuvi. Har bir kirish/o'zgartirish AuditLog ga yoziladi.
    Tibbiy tafsilotlar (tashxis, davolash, izoh) shifrlangan holda saqlanadi.
    """

    RECORD_TYPES = [
        ("visit", "Tashrif"),
        ("diagnosis", "Tashxis"),
        ("prescription", "Retsept"),
        ("lab_result", "Laboratoriya natijasi"),
        ("procedure", "Muolaja"),
    ]

    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name="records")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="authored_records"
    )
    record_type = models.CharField(max_length=20, choices=RECORD_TYPES, default="visit")
    title = models.CharField(max_length=255)

    # Nozik klinik ma'lumotlar - shifrlangan
    diagnosis = EncryptedTextField(blank=True)
    treatment = EncryptedTextField(blank=True)
    notes = EncryptedTextField(blank=True)

    visit_date = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-visit_date"]

    def __str__(self):
        return f"{self.title} - {self.patient} ({self.visit_date:%Y-%m-%d})"


class AuditLog(models.Model):
    """
    Tibbiy ma'lumotlarga har qanday kirish yoki o'zgartirish shu yerga yoziladi.
    Bu yozuvlar o'zgartirilmaydi (immutable) - hech qanday UPDATE/DELETE API yo'q,
    shifokor/administrator harakatlari doim kuzatilishini ta'minlaydi.
    """

    ACTIONS = [
        ("view", "Ko'rish"),
        ("create", "Yaratish"),
        ("update", "Tahrirlash"),
        ("delete", "O'chirish"),
        ("login", "Kirish"),
        ("login_failed", "Muvaffaqiyatsiz kirish"),
        ("export", "Eksport qilish"),
    ]

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="audit_actions"
    )
    action = models.CharField(max_length=20, choices=ACTIONS)
    patient = models.ForeignKey(
        Patient, on_delete=models.SET_NULL, null=True, blank=True, related_name="audit_entries"
    )
    medical_record = models.ForeignKey(
        MedicalRecord, on_delete=models.SET_NULL, null=True, blank=True
    )
    detail = models.CharField(max_length=255, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        return f"[{self.timestamp:%Y-%m-%d %H:%M}] {self.actor} -> {self.action}"