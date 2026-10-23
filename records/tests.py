"""
Muddati o'tgan retseptlarni yopish davriy vazifasi va kasallik tarixi PDF testlari.
Ishlatish: python manage.py test records --settings=config.settings_test
"""
import tempfile
from datetime import timedelta
from io import BytesIO

from cryptography.fernet import Fernet
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import TestCase, override_settings
from django.utils import timezone
from PIL import Image
from rest_framework.test import APITestCase

from users.models import Role, User

from .models import AuditLog, MedicalRecord, Patient, Prescription
from .pdf_export import _text
from .tasks import deactivate_expired_prescriptions


class DeactivateExpiredPrescriptionsTests(TestCase):
    def setUp(self):
        self.today = timezone.now().date()
        self.doctor = User.objects.create_user(username="doc", password="x", role=Role.DOCTOR)
        patient_user = User.objects.create_user(username="pat", password="x", role=Role.PATIENT)
        self.patient = Patient.objects.get(user=patient_user)

    def prescription(self, days_ago_started, duration_days, is_active=True):
        return Prescription.objects.create(
            patient=self.patient, prescribed_by=self.doctor, medication_name="Test", dosage="1",
            duration_days=duration_days, start_date=self.today - timedelta(days=days_ago_started),
            is_active=is_active,
        )

    def test_only_expired_prescriptions_closed(self):
        expired = self.prescription(days_ago_started=20, duration_days=10)      # 10 kun oldin tugagan
        ends_today = self.prescription(days_ago_started=10, duration_days=10)   # bugun tugaydi - faol qoladi
        future = self.prescription(days_ago_started=1, duration_days=10)

        self.assertEqual(deactivate_expired_prescriptions(), 1)
        for obj in (expired, ends_today, future):
            obj.refresh_from_db()
        self.assertFalse(expired.is_active)
        self.assertTrue(ends_today.is_active)
        self.assertTrue(future.is_active)

    def test_each_closure_writes_system_audit_entry(self):
        first = self.prescription(days_ago_started=40, duration_days=5)
        second = self.prescription(days_ago_started=30, duration_days=5)
        before = AuditLog.objects.count()

        self.assertEqual(deactivate_expired_prescriptions(), 2)
        entries = AuditLog.objects.order_by("id")[before:]
        self.assertEqual(len(entries), 2)
        for entry in entries:
            self.assertIsNone(entry.actor)
            self.assertEqual(entry.action, "update")
            self.assertEqual(entry.patient, self.patient)
            self.assertIn("avtomatik yopildi", entry.detail)
        details = " ".join(e.detail for e in entries)
        self.assertIn(f"#{first.pk} ", details)
        self.assertIn(f"#{second.pk} ", details)

    def test_second_run_changes_nothing(self):
        self.prescription(days_ago_started=20, duration_days=10)
        deactivate_expired_prescriptions()
        audit_count = AuditLog.objects.count()
        self.assertEqual(deactivate_expired_prescriptions(), 0)
        self.assertEqual(AuditLog.objects.count(), audit_count)

    def test_already_inactive_untouched(self):
        inactive = self.prescription(days_ago_started=20, duration_days=10, is_active=False)
        self.assertEqual(deactivate_expired_prescriptions(), 0)
        self.assertFalse(AuditLog.objects.filter(detail__contains=f"#{inactive.pk} ").exists())


class BeatScheduleTests(TestCase):
    def test_schedule_defined_in_settings(self):
        entry = settings.CELERY_BEAT_SCHEDULE["deactivate-expired-prescriptions"]
        self.assertEqual(entry["task"], "records.tasks.deactivate_expired_prescriptions")
        self.assertEqual(entry["schedule"].minute, {30})
        self.assertEqual(entry["schedule"].hour, {0})

    def test_database_scheduler_syncs_and_does_not_run_immediately(self):
        from django_celery_beat.models import PeriodicTask
        from django_celery_beat.schedulers import DatabaseScheduler

        from config.celery import app

        scheduler = DatabaseScheduler(app=app, lazy=False)
        task = PeriodicTask.objects.get(name="deactivate-expired-prescriptions")
        self.assertTrue(task.enabled)
        self.assertEqual((task.crontab.minute, task.crontab.hour), ("30", "0"))
        self.assertIsNone(task.last_run_at)
        is_due, _next = scheduler.schedule["deactivate-expired-prescriptions"].is_due()
        self.assertFalse(is_due)


def png_bytes():
    buf = BytesIO()
    Image.new("RGB", (16, 16), (200, 30, 30)).save(buf, "PNG")
    return buf.getvalue()


# MedicalRecord tibbiy maydonlari shifrlangan: testlar uchun bir martalik kalit
@override_settings(FIELD_ENCRYPTION_KEY=Fernet.generate_key().decode())
class PatientHistoryPDFTests(APITestCase):
    """Foydalanuvchi matni reportlab belgilashi sifatida talqin qilinmasin (ZAP 2026-10-23: history-pdf 500)."""

    def setUp(self):
        self.doctor = User.objects.create_user(username="pdfdoc", password="x", role=Role.DOCTOR)
        patient_user = User.objects.create_user(username="pdfpat", password="x", role=Role.PATIENT)
        self.patient = Patient.objects.get(user=patient_user)
        self.client.force_authenticate(self.doctor)

    def record(self, **fields):
        data = {"title": "Ko'rik", "diagnosis": "Gripp", "treatment": "Dam olish", "notes": ""}
        data.update(fields)
        return MedicalRecord.objects.create(
            patient=self.patient, created_by=self.doctor, visit_date=timezone.now(), **data
        )

    def get_pdf(self):
        response = self.client.get(f"/api/records/patients/{self.patient.pk}/history-pdf/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        return b"".join(response.streaming_content)

    def test_unclosed_tag_does_not_break_pdf(self):
        self.record(title="<b>Sarlavha", diagnosis="</p><scrIpt>alert(1);</scRipt><p>", treatment="<font", notes="<i>")
        self.assertTrue(self.get_pdf().startswith(b"%PDF"))

    def test_img_tag_does_not_embed_server_file(self):
        with tempfile.NamedTemporaryFile(suffix=".png") as f:
            f.write(png_bytes())
            f.flush()
            self.record(diagnosis=f'<img src="{f.name}" width="40" height="40"/>')
            self.assertNotIn(b"/Subtype /Image", self.get_pdf())

    def test_link_tag_does_not_create_link(self):
        self.record(notes='<a href="https://evil.example/">bosing</a>')
        self.assertNotIn(b"/URI", self.get_pdf())

    def test_doctor_name_is_escaped(self):
        self.doctor.first_name = "<b>Ism"
        self.doctor.save(update_fields=["first_name"])
        self.record()
        self.assertTrue(self.get_pdf().startswith(b"%PDF"))

    def test_text_helper_escapes_and_keeps_line_breaks(self):
        self.assertEqual(_text("a < b & c > d"), "a &lt; b &amp; c &gt; d")
        self.assertEqual(_text("1-qator\r\n2-qator\n<b>"), "1-qator<br/>2-qator<br/>&lt;b&gt;")

    def set_avatar(self, name, data):
        user = self.patient.user
        user.avatar.name = default_storage.save(name, ContentFile(data))
        user.save(update_fields=["avatar"])

    def test_non_image_avatar_does_not_break_pdf(self):
        self.record()
        for name, data in (("avatars/x.jpg", b"<?php echo 1; ?>"), ("avatars/y.bin", b"\x00" * 64)):
            with self.subTest(name=name):
                self.set_avatar(name, data)
                self.assertNotIn(b"/Subtype /Image", self.get_pdf())

    def test_valid_avatar_still_embedded(self):
        self.record()
        self.set_avatar("avatars/ok.png", png_bytes())
        self.assertIn(b"/Subtype /Image", self.get_pdf())
