"""
Navbat (onlayn, registratura, kiosk) testlari.
Ishlatish: python manage.py test appointments --settings=config.settings_test
"""
from datetime import datetime, time, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from django.db import connection
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from records.models import AuditLog, Patient
from users.models import Role, User

from .models import DoctorProfile, QueueTicket
from .services import queue_state, service_minutes

TZ = ZoneInfo("Asia/Tashkent")
DAY = datetime(2026, 11, 2, tzinfo=TZ).date()


def at(h, m=0):
    return datetime(2026, 11, 2, h, m, tzinfo=TZ)


class QueueTestBase(APITestCase):
    def setUp(self):
        self.clock = mock.patch("django.utils.timezone.now", return_value=at(10, 0))
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.doctor = User.objects.create_user(username="doc", password="x", role=Role.DOCTOR, first_name="Aziz")
        self.doctor2 = User.objects.create_user(username="doc2", password="x", role=Role.DOCTOR)
        self.profile = DoctorProfile.objects.get(user=self.doctor)
        self.profile.specialty, self.profile.avg_minutes = "Terapevt", 15
        self.profile.work_start, self.profile.work_end = time(9, 0), time(17, 0)
        self.profile.save()
        self.pat_user = User.objects.create_user(username="pat", password="x", role=Role.PATIENT)
        self.patient = Patient.objects.get(user=self.pat_user)
        self.pat2_user = User.objects.create_user(username="pat2", password="x", role=Role.PATIENT)
        self.admin = User.objects.create_user(username="adm", password="x", role=Role.ADMIN)

    def set_time(self, h, m=0):
        self.now.return_value = at(h, m)

    def take(self, user=None, doctor=None):
        self.client.force_authenticate(user or self.pat_user)
        return self.client.post("/api/queue/tickets/", {"doctor": (doctor or self.doctor).id}, format="json")

    def registry(self, name="", doctor=None):
        self.client.force_authenticate(self.admin)
        return self.client.post("/api/queue/registry/", {"doctor": (doctor or self.doctor).id, "visitor_name": name}, format="json")


class NumberingAndEtaTests(QueueTestBase):
    def test_profile_created_for_doctor(self):
        self.assertTrue(DoctorProfile.objects.filter(user=self.doctor2).exists())
        self.assertFalse(DoctorProfile.objects.filter(user=self.pat_user).exists())

    def test_online_and_walk_in_share_one_sequence(self):
        self.assertEqual(self.registry("Ali").data["number"], 1)
        self.assertEqual(self.take().data["number"], 2)
        self.assertEqual(self.registry().data["number"], 3)
        # boshqa shifokorning raqamlari alohida
        self.assertEqual(self.registry(doctor=self.doctor2).data["number"], 1)

    def test_eta_uses_queue_length(self):
        self.registry()
        self.registry()
        r = self.take()
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        self.assertEqual(r.data["position"], 3)
        self.assertEqual(r.data["ahead"], 2)
        self.assertEqual(r.data["eta"], "2026-11-02T10:30+05:00")

    def test_eta_not_before_work_start(self):
        self.set_time(7, 30)
        r = self.take()
        self.assertEqual(r.data["eta"], "2026-11-02T09:00+05:00")

    def test_blended_duration_moves_towards_actual(self):
        for i in range(3):
            QueueTicket.objects.create(doctor=self.doctor, date=DAY, number=i + 1, source="registry",
                                       status=QueueTicket.DONE, called_at=at(9, 0), finished_at=at(9, 30))
        # (15*3 + 30*3) / 6 = 22.5
        self.assertAlmostEqual(service_minutes(self.profile, DAY), 22.5)

    def test_current_patient_remaining_time_counts(self):
        QueueTicket.objects.create(doctor=self.doctor, date=DAY, number=1, source="registry",
                                   status=QueueTicket.CALLED, called_at=at(9, 55))
        state = queue_state(self.profile)
        self.assertEqual(state.next_eta, at(10, 10))  # 15 - 5 daqiqa qoldi

    def test_full_day_rejects_online_but_registry_allowed(self):
        self.set_time(16, 50)
        self.registry()
        r = self.take()
        self.assertEqual(r.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(r.data["code"], "full")
        self.assertEqual(self.registry().status_code, status.HTTP_201_CREATED)

    def test_doctor_not_accepting(self):
        self.profile.accepts_queue = False
        self.profile.save()
        self.assertEqual(self.take().data["code"], "closed")
        self.client.force_authenticate(self.pat_user)
        doctors = self.client.get("/api/queue/doctors/").data
        self.assertFalse(next(d for d in doctors if d["id"] == self.doctor.id)["accepting"])

    def test_new_day_starts_from_one(self):
        self.take()
        self.set_time(10, 0)
        self.now.return_value = at(10, 0) + timedelta(days=1)
        self.assertEqual(self.take().data["number"], 1)


class PatientRulesTests(QueueTestBase):
    def test_duplicate_ticket_same_doctor_rejected(self):
        self.take()
        r = self.take()
        self.assertEqual(r.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(r.data["code"], "duplicate")

    def test_my_tickets_and_cancel(self):
        t = self.take().data
        self.client.force_authenticate(self.pat_user)
        mine = self.client.get("/api/queue/tickets/my/").data
        self.assertEqual([m["id"] for m in mine], [t["id"]])
        r = self.client.post(f"/api/queue/tickets/{t['id']}/cancel/")
        self.assertEqual(r.data["status"], "cancelled")
        # bekor qilingandan keyin qayta olish mumkin
        self.assertEqual(self.take().status_code, status.HTTP_201_CREATED)

    def test_cannot_cancel_or_see_others_ticket(self):
        t = self.take().data
        self.client.force_authenticate(self.pat2_user)
        self.assertEqual(self.client.post(f"/api/queue/tickets/{t['id']}/cancel/").status_code, 404)
        self.assertEqual(self.client.get("/api/queue/tickets/my/").data, [])

    def test_doctor_list_has_no_patient_names(self):
        self.registry("Maxfiy Ism")
        self.client.force_authenticate(self.pat2_user)
        body = str(self.client.get("/api/queue/doctors/").data)
        self.assertNotIn("Maxfiy", body)

    def test_roles(self):
        self.assertEqual(self.take(user=self.doctor2).status_code, status.HTTP_403_FORBIDDEN)
        self.client.force_authenticate(self.pat_user)
        self.assertEqual(self.client.post("/api/queue/registry/", {"doctor": self.doctor.id}).status_code, 403)
        self.assertEqual(self.client.get("/api/queue/doctor/").status_code, 403)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get("/api/queue/doctors/").status_code, 401)

    def test_online_ticket_is_audited(self):
        self.take()
        self.assertTrue(AuditLog.objects.filter(actor=self.pat_user, action="create", patient=self.patient,
                                                detail__startswith="Onlayn navbat").exists())

    def test_visitor_name_encrypted_at_rest(self):
        t = self.registry("Karimov Anvar").data
        with connection.cursor() as c:
            c.execute("SELECT visitor_name FROM appointments_queueticket WHERE id=%s", [t["id"]])
            raw = c.fetchone()[0]
        self.assertNotIn("Karimov", raw)
        self.assertEqual(QueueTicket.objects.get(pk=t["id"]).visitor_name, "Karimov Anvar")


class DoctorActionsTests(QueueTestBase):
    def test_call_next_flow_and_eta_update(self):
        self.registry("A")
        self.registry("B")
        t3 = self.take().data
        self.client.force_authenticate(self.doctor)
        r = self.client.post("/api/queue/doctor/next/")
        self.assertEqual(r.data["called"]["number"], 1)
        self.set_time(10, 20)
        r = self.client.post("/api/queue/doctor/next/")
        self.assertEqual(r.data["called"]["number"], 2)
        first = QueueTicket.objects.get(number=1, doctor=self.doctor)
        self.assertEqual(first.status, QueueTicket.DONE)
        # bemor endi birinchi o'rinda
        self.client.force_authenticate(self.pat_user)
        mine = self.client.get("/api/queue/tickets/my/").data[0]
        self.assertEqual(mine["id"], t3["id"])
        self.assertEqual(mine["position"], 1)

    def test_doctor_sees_names(self):
        self.registry("Ali Valiyev")
        self.take()
        self.client.force_authenticate(self.doctor)
        data = self.client.get("/api/queue/doctor/").data
        self.assertEqual([t["name"] for t in data["tickets"]], ["Ali Valiyev", "pat"])

    def test_no_show_only_own_queue(self):
        t = self.registry(doctor=self.doctor2).data
        self.client.force_authenticate(self.doctor)
        self.assertEqual(self.client.post(f"/api/queue/doctor/tickets/{t['id']}/no-show/").status_code, 404)
        self.client.force_authenticate(self.doctor2)
        self.assertEqual(self.client.post(f"/api/queue/doctor/tickets/{t['id']}/no-show/").data["status"], "no_show")

    def test_settings_validation(self):
        self.client.force_authenticate(self.doctor)
        r = self.client.patch("/api/queue/doctor/settings/", {"work_start": "18:00", "work_end": "09:00"}, format="json")
        self.assertEqual(r.status_code, 400)
        r = self.client.patch("/api/queue/doctor/settings/", {"avg_minutes": 20, "room": "12"}, format="json")
        self.assertEqual(r.data["avg_minutes"], 20)


class KioskTests(QueueTestBase):
    def kiosk(self, key=None):
        self.client.force_authenticate(None)
        headers = {"HTTP_X_KIOSK_KEY": key} if key is not None else {}
        return self.client.post("/api/queue/kiosk/tickets/", {"doctor": self.doctor.id}, format="json", **headers)

    @override_settings(QUEUE_KIOSK_KEY="")
    def test_kiosk_disabled_without_key(self):
        self.assertEqual(self.kiosk("anything").status_code, 403)

    @override_settings(QUEUE_KIOSK_KEY="s3cret-kiosk")
    def test_kiosk_key_required(self):
        self.assertEqual(self.kiosk().status_code, 403)
        self.assertEqual(self.kiosk("wrong").status_code, 403)
        r = self.kiosk("s3cret-kiosk")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.data["source"], "kiosk")
        self.client.force_authenticate(None)
        r = self.client.get("/api/queue/kiosk/doctors/", HTTP_X_KIOSK_KEY="s3cret-kiosk")
        self.assertEqual(r.status_code, 200)
