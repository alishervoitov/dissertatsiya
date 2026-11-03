"""
Shubhali faollik ogohlantirishlari. Audit jurnalidagi har bir xodim harakati oddiy
qoidalar bilan tekshiriladi (detector.py), qoida buzilsa administrator uchun
ogohlantirish yaratiladi yoki mavjudining hisoblagichi oshiriladi.
"""
from django.conf import settings
from django.db import models


class SecurityAlert(models.Model):
    MASS_ACCESS = "mass_access"
    OFF_HOURS = "off_hours"
    BULK_EXPORT = "bulk_export"
    AUDIT_TAMPER = "audit_tamper"
    RULES = [
        (MASS_ACCESS, "Qisqa vaqtda ko'p bemor kartasi ochildi"),
        (OFF_HOURS, "Ish vaqtidan tashqari murojaat"),
        (BULK_EXPORT, "Ko'p PDF eksport"),
        (AUDIT_TAMPER, "Audit jurnali yaxlitligi buzilgan"),
    ]
    SEVERITIES = [("low", "Past"), ("medium", "O'rta"), ("high", "Yuqori")]
    OPEN, ACKED = "open", "acknowledged"
    STATUSES = [(OPEN, "Ochiq"), (ACKED, "Ko'rib chiqilgan")]

    rule = models.CharField(max_length=20, choices=RULES)
    severity = models.CharField(max_length=10, choices=SEVERITIES)
    status = models.CharField(max_length=15, choices=STATUSES, default=OPEN, db_index=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="security_alerts",
    )
    actor_role = models.CharField(max_length=10, blank=True)
    count = models.PositiveIntegerField(default=1, help_text="Qoidaga tushgan holatlar soni")
    detail = models.CharField(max_length=255, blank=True)
    first_seen = models.DateTimeField()
    last_seen = models.DateTimeField()
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-last_seen"]
        indexes = [models.Index(fields=["rule", "actor", "status"])]

    def __str__(self):
        return f"{self.get_rule_display()} - {self.actor} ({self.count})"
