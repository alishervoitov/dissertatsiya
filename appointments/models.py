"""
Shifokor qabuliga navbat: jonli (registratura, kiosk) va onlayn navbat bitta
ketma-ketlikda raqamlanadi. Navbat faqat joriy kun uchun beriladi.
"""
from datetime import time

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from records.encryption import EncryptedCharField


class Specialty(models.TextChoices):
    """Mutaxassisliklar ro'yxati: bemor shu bo'yicha qidiradi. Nomlar frontendda 3 tilga tarjima qilinadi."""

    THERAPIST = "therapist", "Terapevt"
    PEDIATRICIAN = "pediatrician", "Pediatr"
    DENTIST = "dentist", "Stomatolog"
    OPHTHALMOLOGIST = "ophthalmologist", "Oftalmolog (okulist)"
    CARDIOLOGIST = "cardiologist", "Kardiolog"
    NEUROLOGIST = "neurologist", "Nevrolog"
    ENT = "ent", "LOR (otorinolaringolog)"
    SURGEON = "surgeon", "Jarroh"
    GYNECOLOGIST = "gynecologist", "Ginekolog"
    DERMATOLOGIST = "dermatologist", "Dermatolog"
    ENDOCRINOLOGIST = "endocrinologist", "Endokrinolog"
    GASTROENTEROLOGIST = "gastroenterologist", "Gastroenterolog"
    UROLOGIST = "urologist", "Urolog"
    TRAUMATOLOGIST = "traumatologist", "Travmatolog-ortoped"
    OTHER = "other", "Boshqa"


class DoctorProfile(models.Model):
    """Shifokorning qabul sozlamalari: mutaxassislik, xona, ish vaqti, o'rtacha qabul davomiyligi."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="doctor_profile",
        limit_choices_to={"role": "doctor"},
    )
    specialty = models.CharField(max_length=30, choices=Specialty.choices, blank=True)
    room = models.CharField(max_length=20, blank=True, help_text="Xona raqami")
    work_start = models.TimeField(default=time(9, 0))
    work_end = models.TimeField(default=time(17, 0))
    avg_minutes = models.PositiveSmallIntegerField(
        default=15, validators=[MinValueValidator(3), MaxValueValidator(120)],
        help_text="Bitta qabulning o'rtacha davomiyligi (daqiqa)",
    )
    accepts_queue = models.BooleanField(default=True, help_text="Bugun navbat qabul qiladimi")

    def __str__(self):
        name = self.user.get_full_name() or self.user.username
        return f"{name} ({self.specialty or 'shifokor'})"


class QueueTicket(models.Model):
    SOURCE_ONLINE = "online"
    SOURCE_REGISTRY = "registry"
    SOURCE_KIOSK = "kiosk"
    SOURCES = [
        (SOURCE_ONLINE, "Onlayn"),
        (SOURCE_REGISTRY, "Registratura"),
        (SOURCE_KIOSK, "Kiosk"),
    ]

    WAITING = "waiting"
    CALLED = "called"
    DONE = "done"
    NO_SHOW = "no_show"
    CANCELLED = "cancelled"
    STATUSES = [
        (WAITING, "Kutmoqda"),
        (CALLED, "Qabulda"),
        (DONE, "Yakunlandi"),
        (NO_SHOW, "Kelmadi"),
        (CANCELLED, "Bekor qilindi"),
    ]
    ACTIVE = (WAITING, CALLED)

    doctor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="queue_tickets",
        limit_choices_to={"role": "doctor"},
    )
    date = models.DateField(db_index=True)
    number = models.PositiveIntegerField()
    source = models.CharField(max_length=10, choices=SOURCES)
    status = models.CharField(max_length=10, choices=STATUSES, default=WAITING, db_index=True)

    # Onlayn navbatda bemor profili; jonli navbatda hisob bo'lmasligi mumkin
    patient = models.ForeignKey(
        "records.Patient", on_delete=models.SET_NULL, null=True, blank=True, related_name="queue_tickets",
    )
    visitor_name = EncryptedCharField(blank=True, help_text="Registraturada kiritilgan ism (ixtiyoriy)")
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="issued_queue_tickets",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    # Bemor shifoxonaga kelgan vaqt. Jonli navbatda chipta olingan payt; onlayn navbatda bemor
    # "Keldim" tugmasini bosgan yoki registratura belgilagan payt. Kelmaganlar navbatni ushlab turmaydi.
    arrived_at = models.DateTimeField(null=True, blank=True)
    called_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["date", "number"]
        constraints = [
            models.UniqueConstraint(fields=["doctor", "date", "number"], name="queue_ticket_unique_number"),
        ]

    @property
    def arrived(self):
        return self.arrived_at is not None

    def __str__(self):
        return f"№{self.number} {self.date} ({self.get_status_display()})"
