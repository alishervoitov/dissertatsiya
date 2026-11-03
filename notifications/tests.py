"""
Sayt ichidagi bildirishnomalar testlari.
Ishlatish: python manage.py test notifications --settings=config.settings_test
"""
from datetime import datetime, time, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from django.utils import timezone
from rest_framework.test import APITestCase

from appointments.models import DoctorProfile
from records.models import Patient, Prescription
from users.models import Role, User

from .models import Notification

TZ = ZoneInfo("Asia/Tashkent")


class NotificationTests(APITestCase):
    def setUp(self):
        self.clock = mock.patch("django.utils.timezone.now", return_value=datetime(2026, 11, 3, 10, 0, tzinfo=TZ))
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.lab = User.objects.create_user(username="lab", password="x", role=Role.LAB)
        self.doctor = User.objects.create_user(username="doc", password="x", role=Role.DOCTOR, first_name="Aziz")
        prof = DoctorProfile.objects.get(user=self.doctor)
        prof.work_start, prof.work_end, prof.room = time(8), time(18), "12"
        prof.save()
        self.admin = User.objects.create_user(username="adm", password="x", role=Role.ADMIN)
        self.pats = []
        for i in range(4):
            u = User.objects.create_user(username=f"p{i}", password="x", role=Role.PATIENT)
            self.pats.append(u)

    def mine(self, user):
        self.client.force_authenticate(user)
        return self.client.get("/api/notifications/").data

    def test_lab_ready(self):
        p = Patient.objects.get(user=self.pats[0])
        self.client.force_authenticate(self.lab)
        self.client.post("/api/labs/results/", {
            "patient": p.id, "panel": "cbc", "title": "Umumiy qon tahlili", "collected_at": timezone.now().isoformat(),
            "values": [{"name": "HGB", "value": "112", "ref_low": "130", "ref_high": "160"}],
        }, format="json")
        data = self.mine(self.pats[0])
        self.assertEqual(data["unread"], 1)
        n = data["results"][0]
        self.assertEqual((n["kind"], n["params"]["title"], n["params"]["abnormal"]), ("lab_ready", "Umumiy qon tahlili", 1))
        self.assertEqual(self.mine(self.pats[1])["unread"], 0)       # boshqa bemorga emas

    def take(self, user):
        self.client.force_authenticate(user)
        t = self.client.post("/api/queue/tickets/", {"doctor": self.doctor.id}, format="json").data
        self.client.post(f"/api/queue/tickets/{t['id']}/arrive/")
        return t

    def test_queue_near_and_called(self):
        for u in self.pats:
            self.take(u)
        self.client.force_authenticate(self.doctor)
        self.client.post("/api/queue/doctor/next/")                   # p0 chaqirildi
        kinds = lambda u: sorted(n["kind"] for n in self.mine(u)["results"])
        self.assertEqual(kinds(self.pats[0]), ["queue_called"])
        self.assertEqual(kinds(self.pats[1]), ["queue_near"])         # oldida 0
        self.assertEqual(kinds(self.pats[2]), ["queue_near"])         # oldida 1
        self.assertEqual(kinds(self.pats[3]), ["queue_near"])         # oldida 2
        self.client.force_authenticate(self.doctor)
        self.client.post("/api/queue/doctor/next/")                   # takror xabar yo'q
        self.assertEqual(kinds(self.pats[2]), ["queue_near"])
        self.assertEqual(kinds(self.pats[1]), ["queue_called", "queue_near"])

    def test_rx_expiring_once(self):
        p = Patient.objects.get(user=self.pats[0])
        today = timezone.now().date()
        Prescription.objects.create(patient=p, prescribed_by=self.doctor, medication_name="Amoksitsillin", dosage="500mg",
                                    duration_days=5, start_date=today - timedelta(days=4))     # ertaga tugaydi
        Prescription.objects.create(patient=p, prescribed_by=self.doctor, medication_name="Vitamin", dosage="1",
                                    duration_days=30, start_date=today)                        # hali uzoq
        first = self.mine(self.pats[0])
        self.assertEqual([n["params"]["medication"] for n in first["results"]], ["Amoksitsillin"])
        self.assertEqual(first["results"][0]["params"]["days_left"], 1)
        self.assertEqual(len(self.mine(self.pats[0])["results"]), 1)   # takrorlanmaydi

    def test_read_and_isolation(self):
        n = Notification.objects.create(user=self.pats[0], kind="lab_ready", params={"title": "X"})
        self.client.force_authenticate(self.pats[1])
        self.assertEqual(self.client.post(f"/api/notifications/{n.id}/read/").status_code, 404)
        self.client.force_authenticate(self.pats[0])
        self.assertTrue(self.client.post(f"/api/notifications/{n.id}/read/").data["is_read"])
        Notification.objects.create(user=self.pats[0], kind="lab_ready", params={"title": "Y"})
        self.assertEqual(self.client.get("/api/notifications/count/").data["unread"], 1)
        self.assertEqual(self.client.post("/api/notifications/read-all/").data["updated"], 1)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get("/api/notifications/").status_code, 401)
