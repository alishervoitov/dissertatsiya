"""
Shubhali faollik qoidalari testlari.
Ishlatish: python manage.py test monitoring --settings=config.settings_test
"""
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

from django.test import override_settings
from rest_framework.test import APITestCase

from records.audit import log_audit
from records.models import AuditLog, Patient
from users.models import Role, User

from .models import SecurityAlert

TZ = ZoneInfo("Asia/Tashkent")


def at(h, m=0, day=3):
    return datetime(2026, 11, day, h, m, tzinfo=TZ)


@override_settings(SECURITY_ALERTS={"MASS_ACCESS_PATIENTS": 3, "MASS_ACCESS_MINUTES": 10, "EXPORT_COUNT": 2, "EXPORT_MINUTES": 30})
class DetectorTests(APITestCase):
    def setUp(self):
        self.clock = mock.patch("django.utils.timezone.now", return_value=at(11))
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.doctor = User.objects.create_user(username="d", password="x", role=Role.DOCTOR)
        self.admin = User.objects.create_user(username="a", password="x", role=Role.ADMIN)
        self.patients = []
        for i in range(5):
            u = User.objects.create_user(username=f"p{i}", password="x", role=Role.PATIENT)
            self.patients.append(Patient.objects.get(user=u))

    def alerts(self, rule=None):
        qs = SecurityAlert.objects.all()
        return qs.filter(rule=rule) if rule else qs

    def test_mass_access_raised_once_and_counted(self):
        for p in self.patients[:2]:
            log_audit(self.doctor, "view", patient=p)
        self.assertEqual(self.alerts("mass_access").count(), 0)
        log_audit(self.doctor, "view", patient=self.patients[2])
        log_audit(self.doctor, "view", patient=self.patients[3])
        self.assertEqual(self.alerts("mass_access").count(), 1)  # takrorlanmaydi
        a = self.alerts("mass_access").get()
        self.assertEqual((a.actor, a.severity), (self.doctor, "high"))
        self.assertGreaterEqual(a.count, 4)

    def test_same_patient_many_times_is_not_mass_access(self):
        for _ in range(10):
            log_audit(self.doctor, "view", patient=self.patients[0])
        self.assertEqual(self.alerts("mass_access").count(), 0)

    def test_old_access_outside_window_ignored(self):
        self.now.return_value = at(9)
        for p in self.patients[:2]:
            log_audit(self.doctor, "view", patient=p)
        self.now.return_value = at(11)
        log_audit(self.doctor, "view", patient=self.patients[2])
        self.assertEqual(self.alerts("mass_access").count(), 0)

    def test_off_hours_once_per_night(self):
        self.now.return_value = at(23, 10)
        log_audit(self.doctor, "view", patient=self.patients[0])
        self.now.return_value = at(2, 30, day=4)
        log_audit(self.doctor, "view", patient=self.patients[1])
        self.assertEqual(self.alerts("off_hours").count(), 1)
        self.assertEqual(self.alerts("off_hours").get().count, 2)
        self.now.return_value = at(23, 0, day=4)                  # keyingi kecha - yangi ogohlantirish
        log_audit(self.doctor, "view", patient=self.patients[2])
        self.assertEqual(self.alerts("off_hours").count(), 2)

    def test_daytime_no_alert(self):
        self.now.return_value = at(14)
        log_audit(self.doctor, "view", patient=self.patients[0])
        self.assertEqual(self.alerts().count(), 0)

    def test_patient_own_actions_never_alert(self):
        self.now.return_value = at(23, 30)
        log_audit(self.patients[0].user, "view", patient=self.patients[0])
        self.assertEqual(self.alerts().count(), 0)

    def test_bulk_export(self):
        log_audit(self.doctor, "export", patient=self.patients[0])
        self.assertEqual(self.alerts("bulk_export").count(), 0)
        log_audit(self.doctor, "export", patient=self.patients[0])
        self.assertEqual(self.alerts("bulk_export").count(), 1)

    def test_detector_error_does_not_break_audit(self):
        with mock.patch("monitoring.detector.check", side_effect=RuntimeError("boom")):
            log_audit(self.doctor, "view", patient=self.patients[0])
        self.assertTrue(AuditLog.objects.filter(actor=self.doctor).exists())

    def test_api_admin_only_and_ack(self):
        self.now.return_value = at(23, 30)
        log_audit(self.doctor, "view", patient=self.patients[0])
        alert = self.alerts().get()
        self.client.force_authenticate(self.doctor)
        self.assertEqual(self.client.get("/api/monitoring/alerts/").status_code, 403)
        self.client.force_authenticate(self.admin)
        r = self.client.get("/api/monitoring/alerts/?status=open")
        self.assertEqual(r.data["open_count"], 1)
        self.assertEqual(r.data["results"][0]["rule"], "off_hours")
        r = self.client.post(f"/api/monitoring/alerts/{alert.id}/ack/")
        self.assertEqual(r.data["status"], "acknowledged")
        self.assertEqual(self.client.get("/api/monitoring/alerts/count/").data["open_count"], 0)
        # ko'rib chiqilgandan keyin shu kechadagi yangi murojaat yangi ogohlantirish ochadi
        log_audit(self.doctor, "view", patient=self.patients[1])
        self.assertEqual(SecurityAlert.objects.filter(status="open").count(), 1)
