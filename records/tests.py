"""
Muddati o'tgan retseptlarni yopish davriy vazifasi testlari.
Ishlatish: python manage.py test records --settings=config.settings_test
"""
from datetime import timedelta

from django.conf import settings
from django.test import TestCase
from django.utils import timezone

from users.models import Role, User

from .models import AuditLog, Patient, Prescription
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
