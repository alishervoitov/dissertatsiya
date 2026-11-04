from django.conf import settings
from django.db import models, transaction
from django.utils import timezone

from .encryption import EncryptedCharField, EncryptedTextField
from .storage import protected_storage

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
    # Vaqt save() da qo'yiladi: u xeshga kiradi, shuning uchun bazaga yozilishidan oldin ma'lum bo'lishi kerak
    timestamp = models.DateTimeField(editable=False)

    # --- Yaxlitlik (xesh zanjiri, qarang records/audit_chain.py) ---
    # FK'lar foydalanuvchi/bemor o'chirilganda NULL bo'lishi mumkin, shuning uchun xeshga
    # ularning yozilgan paytdagi nusxasi kiradi.
    actor_snapshot = models.CharField(max_length=150, blank=True, editable=False)
    patient_ref = models.IntegerField(null=True, blank=True, editable=False)
    record_ref = models.IntegerField(null=True, blank=True, editable=False)
    prev_hash = models.CharField(max_length=64, blank=True, editable=False)
    entry_hash = models.CharField(max_length=64, blank=True, editable=False, db_index=True)

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        return f"[{self.timestamp:%Y-%m-%d %H:%M}] {self.actor} -> {self.action}"

    def save(self, *args, **kwargs):
        """Faqat yangi yozuv qo'shish mumkin; u zanjirning oxiriga ulanadi."""
        if not self._state.adding:
            raise PermissionError("Audit yozuvini o'zgartirib bo'lmaydi.")
        from . import audit_chain

        if self.timestamp is None:
            self.timestamp = timezone.now()
        self.detail = (self.detail or "")[:255]
        self.user_agent = (self.user_agent or "")[:300]
        self.actor_snapshot = audit_chain.actor_snapshot(self.actor)
        self.patient_ref = self.patient_id
        self.record_ref = self.medical_record_id
        with transaction.atomic():
            audit_chain.lock_chain()
            self.prev_hash = audit_chain.head_hash(type(self))
            self.entry_hash = audit_chain.compute_hash(self, self.prev_hash)
            super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError("Audit yozuvini o'chirib bo'lmaydi.")


class AuditCheckpoint(models.Model):
    """Kunlik nazorat nuqtasi: zanjir oxiri (id, xesh, yozuvlar soni). Oxiridan yozuvlar
    o'chirilsa (zanjirning o'zi buni sezmaydi) shu nuqta orqali aniqlanadi."""

    created_at = models.DateTimeField(auto_now_add=True)
    last_id = models.BigIntegerField()
    last_hash = models.CharField(max_length=64)
    count = models.BigIntegerField()

    class Meta:
        ordering = ["-id"]

    def __str__(self):
        return f"#{self.last_id} {self.last_hash[:12]} ({self.created_at:%Y-%m-%d})"



def attachment_upload_path(instance, filename):
    # Asl nom serverdagi yo'lga tushmaydi: tasodifiy nom va aniqlangan turga mos kengaytma
    import uuid

    from .filetypes import EXTENSIONS

    ext = EXTENSIONS.get(instance.content_type, "bin")
    return f"attachments/{instance.record.patient_id}/{uuid.uuid4().hex}.{ext}"

class RecordAttachment(models.Model):
    """
    Tibbiy yozuvga biriktirilgan fayl (rentgen, tahlil natijasi, boshqa
    hujjat). Fayl himoyalangan xotirada saqlanadi va faqat ruxsat
    tekshiruvidan o'tgan foydalanuvchiga beriladi.
    """

    ALLOWED_CONTENT_TYPES = [
        "image/jpeg", "image/png", "image/webp", "application/pdf",
    ]
    MAX_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB

    record = models.ForeignKey(MedicalRecord, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to=attachment_upload_path, storage=protected_storage)
    original_filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100)
    file_size = models.PositiveIntegerField()
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="uploaded_attachments"
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self):
        return f"{self.original_filename} ({self.record})"


class Prescription(models.Model):
    """
    Bemorga tayinlangan dori-darmon retsepti. MedicalRecord'dan alohida,
    chunki bitta tashrifda bir nechta dori tayinlanishi, va dori qabul
    qilish holati (faol/tugagan) alohida kuzatilishi mumkin.
    """

    FREQUENCY_CHOICES = [
        ("once", "Bir marta"),
        ("daily_1", "Kuniga 1 marta"),
        ("daily_2", "Kuniga 2 marta"),
        ("daily_3", "Kuniga 3 marta"),
        ("daily_4", "Kuniga 4 marta"),
        ("weekly", "Haftada bir marta"),
        ("as_needed", "Zarurat bo'yicha"),
    ]

    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name="prescriptions")
    medical_record = models.ForeignKey(
        MedicalRecord, on_delete=models.SET_NULL, null=True, blank=True, related_name="prescriptions"
    )
    prescribed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="issued_prescriptions"
    )

    medication_name = models.CharField(max_length=200)
    dosage = models.CharField(max_length=100, help_text="Masalan: 500mg")
    frequency = models.CharField(max_length=20, choices=FREQUENCY_CHOICES, default="daily_1")
    duration_days = models.PositiveIntegerField(help_text="Necha kun davomida qabul qilinadi")
    instructions = EncryptedTextField(blank=True, help_text="Qo'shimcha ko'rsatmalar")

    start_date = models.DateField()
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-start_date", "-created_at"]

    def __str__(self):
        return f"{self.medication_name} - {self.patient} ({self.start_date})"


class CareGrant(models.Model):
    """Bemorning roziligi: shu shifokor kartamni ko'ra oladi (muddatli yoki muddatsiz).
    Bemor istalgan vaqtda bekor qiladi; yozuv o'chirilmaydi, tarix saqlanadi."""

    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name="care_grants")
    doctor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="care_grants",
                               limit_choices_to={"role": "doctor"})
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    note = models.CharField(max_length=100, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True, help_text="Bo'sh - muddatsiz")
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["doctor", "patient", "revoked_at"])]

    def __str__(self):
        return f"{self.patient} -> {self.doctor}"


class EmergencyAccess(models.Model):
    """Favqulodda kirish ("break-glass"): roziligi yo'q bemor kartasini shifokor sabab
    yozib qisqa muddatga ochadi. O'zgartirilmaydi; administratorga ogohlantirish, bemorga xabar ketadi."""

    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name="emergency_accesses")
    doctor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="emergency_accesses")
    reason = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["doctor", "patient", "expires_at"])]

    def __str__(self):
        return f"FAVQULODDA {self.doctor} -> {self.patient}"


class PatientPhoto(models.Model):
    """Bemorning 3x4 rasmi - registratura va shifokor shaxsni tasdiqlashi uchun.

    Yuz tasviri shaxsiy ma'lumot: u /media/ da ochiq fayl bo'lib yotmaydi, balki
    boshqa maxfiy maydonlar kabi Fernet bilan shifrlanib bazada saqlanadi. Shuning uchun
    kalit almashtirish (rotate_encryption_key) va shifrlangan zaxira nusxa uni ham qamraydi.
    Alohida jadval - bemorlar ro'yxati so'rovlari rasm baytlarini yuklamasin.
    """

    patient = models.OneToOneField(Patient, on_delete=models.CASCADE, related_name="photo")
    image = EncryptedTextField(help_text="JPEG 600x800, base64")
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                    related_name="+")
    updated_at = models.DateTimeField()

    def __str__(self):
        return f"Rasm: {self.patient}"
