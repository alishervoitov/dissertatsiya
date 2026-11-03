"""
Laboratoriya tahlillari natijalari (qon, siydik, biokimyo va h.k.).
Natijani laborant kiritadi; bemor o'z kabinetida, shifokor bemor sahifasida ko'radi.
Ko'rsatkich qiymatlari va izohlar bazada shifrlangan holda saqlanadi.
"""
import uuid
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import models

from records.encryption import EncryptedCharField, EncryptedTextField


def lab_storage():
    # Chaqiriladigan storage: migratsiyaga server yo'li yozilib qolmaydi
    from records.storage import protected_storage
    return protected_storage


def lab_upload_path(instance, filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "bin"
    return f"lab/{instance.result.patient_id}/{uuid.uuid4().hex}.{ext}"


# Tahlil natijasi necha kun amal qiladi (standart). Laborant har bir natija uchun o'zgartira oladi.
DEFAULT_VALIDITY_DAYS = {"cbc": 10, "urine": 10, "biochem": 14, "custom": 30}


def default_valid_until(panel, collected_at):
    days = getattr(settings, "LAB_VALIDITY_DAYS", DEFAULT_VALIDITY_DAYS).get(panel, DEFAULT_VALIDITY_DAYS["custom"])
    return collected_at.date() + timedelta(days=days)


class LabResult(models.Model):
    PANELS = [
        ("cbc", "Umumiy qon tahlili"),
        ("biochem", "Qon biokimyosi"),
        ("urine", "Umumiy siydik tahlili"),
        ("custom", "Boshqa tahlil"),
    ]

    patient = models.ForeignKey("records.Patient", on_delete=models.CASCADE, related_name="lab_results")
    panel = models.CharField(max_length=20, choices=PANELS, default="custom")
    title = models.CharField(max_length=200)
    collected_at = models.DateTimeField(help_text="Namuna olingan vaqt")
    valid_until = models.DateField(null=True, blank=True, help_text="Shu sanagacha amal qiladi (PDF'ga faqat amaldagilar chiqadi)")
    notes = EncryptedTextField(blank=True, help_text="Laborant izohi")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="entered_lab_results",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-collected_at", "-id"]

    def is_expired(self, today):
        return self.valid_until is not None and self.valid_until < today

    def __str__(self):
        return f"{self.title} ({self.collected_at:%Y-%m-%d})"


def _number(text):
    try:
        return Decimal(str(text).strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        return None


class LabValue(models.Model):
    """Bitta ko'rsatkich: nomi, qiymati (shifrlangan), birligi va me'yor oralig'i."""

    result = models.ForeignKey(LabResult, on_delete=models.CASCADE, related_name="values")
    order = models.PositiveSmallIntegerField(default=0)
    name = models.CharField(max_length=100)
    value = EncryptedCharField()
    unit = models.CharField(max_length=30, blank=True)
    ref_low = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    ref_high = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    ref_text = models.CharField(max_length=60, blank=True, help_text="Sonli bo'lmagan me'yor, masalan: manfiy")

    class Meta:
        ordering = ["order", "id"]

    @property
    def flag(self):
        """low / high / normal / abnormal yoki None (me'yor ko'rsatilmagan)."""
        num = _number(self.value)
        if num is not None and (self.ref_low is not None or self.ref_high is not None):
            if self.ref_low is not None and num < self.ref_low:
                return "low"
            if self.ref_high is not None and num > self.ref_high:
                return "high"
            return "normal"
        if self.ref_text:
            return "normal" if str(self.value).strip().lower() == self.ref_text.strip().lower() else "abnormal"
        return None


class LabAttachment(models.Model):
    """Laboratoriya blankasi (PDF yoki rasm). Himoyalangan xotirada saqlanadi."""

    MAX_SIZE_BYTES = 10 * 1024 * 1024
    ALLOWED_CONTENT_TYPES = ["application/pdf", "image/jpeg", "image/png", "image/webp"]

    result = models.ForeignKey(LabResult, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField(upload_to=lab_upload_path, storage=lab_storage)
    original_filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100)
    file_size = models.PositiveIntegerField()
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.original_filename
