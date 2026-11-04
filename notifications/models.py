"""
Sayt ichidagi bildirishnomalar. Matn bazada emas, frontendda tur (kind) va parametrlar
asosida foydalanuvchi tilida yasaladi. Shu sababli bildirishnomada tibbiy tafsilot
(qiymatlar, tashxis) saqlanmaydi: faqat tahlil nomi, navbat raqami kabi kerakli minimum.
"""
from django.conf import settings
from django.db import models


class Notification(models.Model):
    LAB_READY = "lab_ready"
    QUEUE_NEAR = "queue_near"
    QUEUE_CALLED = "queue_called"
    RX_EXPIRING = "rx_expiring"
    EMERGENCY_ACCESS = "emergency_access"
    KINDS = [
        (LAB_READY, "Tahlil natijasi tayyor"),
        (QUEUE_NEAR, "Navbat yaqinlashdi"),
        (QUEUE_CALLED, "Navbat keldi"),
        (RX_EXPIRING, "Retsept muddati tugayapti"),
        (EMERGENCY_ACCESS, "Kartangizga favqulodda kirildi"),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    kind = models.CharField(max_length=20, choices=KINDS)
    params = models.JSONField(default=dict, blank=True)
    link = models.CharField(max_length=200, blank=True)
    # Bitta hodisa uchun bitta bildirishnoma (masalan "lab:15", "queue_near:42")
    dedupe_key = models.CharField(max_length=80, blank=True)
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["user", "is_read"])]
        constraints = [
            models.UniqueConstraint(fields=["user", "dedupe_key"], condition=~models.Q(dedupe_key=""),
                                    name="notification_unique_dedupe"),
        ]

    def __str__(self):
        return f"{self.user} - {self.kind}"
