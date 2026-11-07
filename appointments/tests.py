"""
Navbat (onlayn, registratura, kiosk) testlari.
Ishlatish: python manage.py test appointments --settings=config.settings_test
"""
from datetime import datetime, time, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from django.core.cache import cache
from django.db import connection
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from records.models import AuditLog, Patient
from users.models import Role, User

from .models import DoctorProfile, QueueTicket
from .services import queue_state, service_minutes

NO_PHOTO = "sinov: kamera yo'q"

TZ = ZoneInfo("Asia/Tashkent")
DAY = datetime(2026, 11, 2, tzinfo=TZ).date()


def at(h, m=0):
    return datetime(2026, 11, 2, h, m, tzinfo=TZ)


class QueueTestBase(APITestCase):
    def setUp(self):
        cache.clear()  # throttle hisoblagichlari testlar orasida to'planmasin
        self.clock = mock.patch("django.utils.timezone.now", return_value=at(10, 0))
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.doctor = User.objects.create_user(username="doc", password="x", role=Role.DOCTOR, first_name="Aziz")
        self.doctor2 = User.objects.create_user(username="doc2", password="x", role=Role.DOCTOR)
        self.profile = DoctorProfile.objects.get(user=self.doctor)
        self.profile.specialty, self.profile.avg_minutes = "therapist", 15
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

    def walk_in_patient(self, name=""):
        """Registraturaga kelgan odam: har chaqiruvda yangi bemor kartasi."""
        self._walk_in = getattr(self, "_walk_in", 0) + 1
        u = User.objects.create_user(username=f"walkin{self._walk_in}", password="x", role=Role.PATIENT, first_name=name)
        return Patient.objects.get(user=u)

    def registry(self, name="", doctor=None, patient=None):
        patient = patient or self.walk_in_patient(name)
        self.client.force_authenticate(self.admin)
        return self.client.post("/api/queue/registry/", {"doctor": (doctor or self.doctor).id, "patient": patient.id,
                                                         "no_photo_reason": NO_PHOTO}, format="json")


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
        from .services import issue_ticket
        t = issue_ticket(self.profile, QueueTicket.SOURCE_KIOSK, visitor_name="Karimov Anvar")
        with connection.cursor() as c:
            c.execute("SELECT visitor_name FROM appointments_queueticket WHERE id=%s", [t.id])
            raw = c.fetchone()[0]
        self.assertNotIn("Karimov", raw)
        self.assertEqual(QueueTicket.objects.get(pk=t.id).visitor_name, "Karimov Anvar")


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
        r = self.client.patch("/api/queue/doctor/settings/", {"avg_minutes": 20, "room": "12", "specialty": "dentist"}, format="json")
        self.assertEqual(r.data["avg_minutes"], 20)
        self.assertEqual(r.data["specialty"], "dentist")
        r = self.client.patch("/api/queue/doctor/settings/", {"specialty": "Tish doktori"}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_doctor_list_returns_specialty_code(self):
        self.client.force_authenticate(self.pat_user)
        doctors = self.client.get("/api/queue/doctors/").data
        self.assertEqual(next(d for d in doctors if d["id"] == self.doctor.id)["specialty"], "therapist")


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


class SpecialtyMigrationTests(QueueTestBase):
    def test_free_text_mapped_to_codes(self):
        from importlib import import_module
        from django.apps import apps as global_apps
        mig = import_module("appointments.migrations.0003_specialty_choices")
        cases = {"Okulist": "ophthalmologist", "стоматолог": "dentist", "Bosh vrach": "other", "therapist": "therapist"}
        for text, code in cases.items():
            DoctorProfile.objects.filter(pk=self.profile.pk).update(specialty=text)
            mig.to_codes(global_apps, None)
            self.assertEqual(DoctorProfile.objects.get(pk=self.profile.pk).specialty, code)


class AdminDoctorSpecialtyTests(QueueTestBase):
    def test_admin_creates_doctor_with_specialty(self):
        self.client.force_authenticate(self.admin)
        r = self.client.post("/api/auth/users/", {
            "username": "newdoc", "first_name": "Kamola", "last_name": "Aliyeva", "email": "",
            "phone": "", "password": "Xk9#mPq2$vLw", "role": "doctor", "specialty": "dentist", "room": "3",
        }, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        p = DoctorProfile.objects.get(user__username="newdoc")
        self.assertEqual((p.specialty, p.room), ("dentist", "3"))
        users = self.client.get("/api/auth/users/").data
        row = next(u for u in users if u["username"] == "newdoc")
        self.assertEqual(row["specialty"], "dentist")
        self.assertIsNone(next(u for u in users if u["username"] == "pat")["specialty"])

    def test_unknown_specialty_rejected(self):
        self.client.force_authenticate(self.admin)
        r = self.client.post("/api/auth/users/", {
            "username": "baddoc", "first_name": "A", "last_name": "B", "password": "Xk9#mPq2$vLw",
            "role": "doctor", "specialty": "Tish doktori",
        }, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("specialty", r.data)

    def test_admin_edits_doctor_specialty(self):
        url = f"/api/queue/doctors/{self.doctor2.id}/profile/"
        self.client.force_authenticate(self.doctor)
        self.assertEqual(self.client.patch(url, {"specialty": "ent"}, format="json").status_code, 403)
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.patch(url, {"specialty": "ent"}, format="json").data["specialty"], "ent")
        self.assertEqual(self.client.patch(f"/api/queue/doctors/{self.pat_user.id}/profile/", {"specialty": "ent"}, format="json").status_code, 404)


class AdminUserStatusTests(QueueTestBase):
    def test_deactivate_and_reactivate(self):
        self.client.force_authenticate(self.admin)
        url = f"/api/auth/users/{self.pat2_user.id}/"
        r = self.client.patch(url, {"is_active": False}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.data["is_active"])
        self.client.force_authenticate(None)
        login = self.client.post("/api/auth/login/", {"username": "pat2", "password": "x"}, format="json")
        self.assertEqual(login.status_code, 401)
        self.client.force_authenticate(self.admin)
        self.assertTrue(self.client.patch(url, {"is_active": True}, format="json").data["is_active"])

    def test_admin_cannot_deactivate_self(self):
        self.client.force_authenticate(self.admin)
        r = self.client.patch(f"/api/auth/users/{self.admin.id}/", {"is_active": False}, format="json")
        self.assertEqual(r.status_code, 400)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)

    def test_list_has_status(self):
        self.client.force_authenticate(self.admin)
        rows = self.client.get("/api/auth/users/").data
        self.assertTrue(all("is_active" in u for u in rows))


class ArrivalTests(QueueTestBase):
    """Faqat shifoxonaga kelgan bemorlar navbatda turadi."""

    def arrive(self, ticket_id, user=None):
        self.client.force_authenticate(user or self.pat_user)
        return self.client.post(f"/api/queue/tickets/{ticket_id}/arrive/")

    def test_absent_online_patient_does_not_delay_walk_in(self):
        self.set_time(8, 30)
        online = self.take().data                      # №1, uyda
        self.assertFalse(online["arrived"])
        self.set_time(9, 0)
        walk_in = self.registry("Ali").data            # №2, shifoxonada
        self.assertEqual(walk_in["number"], 2)
        self.assertEqual(walk_in["position"], 1)
        self.assertEqual(walk_in["eta"], "2026-11-02T09:00+05:00")
        self.client.force_authenticate(self.doctor)
        called = self.client.post("/api/queue/doctor/next/").data["called"]
        self.assertEqual(called["number"], 2)          # kelmagan №1 o'tkazib yuborildi

    def test_arrived_online_patient_takes_place_by_number(self):
        self.set_time(8, 30)
        online = self.take().data                      # №1
        self.set_time(9, 0)
        self.registry("A")                             # №2
        self.registry("B")                             # №3
        self.client.force_authenticate(self.doctor)
        self.client.post("/api/queue/doctor/next/")    # №2 qabulda
        r = self.arrive(online["id"], self.admin)
        self.assertTrue(r.data["arrived"])
        self.client.force_authenticate(self.pat_user)
        mine = self.client.get("/api/queue/tickets/my/").data
        self.assertEqual(next(x for x in mine if x["id"] == online["id"])["position"], 1)  # №3 dan oldin
        self.client.force_authenticate(self.doctor)
        self.assertEqual(self.client.post("/api/queue/doctor/next/").data["called"]["number"], 1)

    def test_pending_online_not_counted_as_waiting(self):
        self.take()
        self.registry()
        self.client.force_authenticate(self.pat2_user)
        d = next(x for x in self.client.get("/api/queue/doctors/").data if x["id"] == self.doctor.id)
        self.assertEqual((d["waiting"], d["pending_online"]), (1, 1))
        self.assertEqual(d["next_eta_walkin"], "2026-11-02T10:15+05:00")
        self.assertEqual(d["next_eta"], "2026-11-02T10:30+05:00")

    def test_arrive_permissions(self):
        t = self.take().data
        self.assertEqual(self.arrive(t["id"], self.pat2_user).status_code, 404)
        self.assertEqual(self.arrive(t["id"], self.doctor2).status_code, 404)
        r = self.arrive(t["id"])                       # bemor uydan bosa olmaydi
        self.assertEqual((r.status_code, r.data["code"]), (403, "qr_required"))
        self.assertEqual(self.arrive(t["id"], self.admin).status_code, 200)
        self.assertEqual(self.arrive(t["id"], self.doctor).status_code, 200)

    def test_cannot_arrive_cancelled(self):
        t = self.take().data
        self.client.post(f"/api/queue/tickets/{t['id']}/cancel/")
        self.assertEqual(self.arrive(t["id"], self.admin).status_code, 409)


class DoctorArrivalSignalTests(QueueTestBase):
    """Shifokor menyusidagi raqam va o'tkazib yuborilgan onlayn bemorlar."""

    def test_count_and_skipped(self):
        self.set_time(8, 30)
        online = self.take().data                      # №1, hali kelmagan
        self.set_time(9, 0)
        self.registry("Ali")                           # №2, shifoxonada
        self.client.force_authenticate(self.doctor)
        self.assertEqual(self.client.get("/api/queue/doctor/count/").data, {"present": 1, "pending": 1})
        r = self.client.post("/api/queue/doctor/next/").data
        self.assertEqual((r["called"]["number"], r["skipped"]), (2, [1]))
        self.assertEqual(self.client.get("/api/queue/doctor/count/").data, {"present": 0, "pending": 1})
        self.client.post(f"/api/queue/tickets/{online['id']}/arrive/")   # shifokor belgilaydi
        self.assertEqual(self.client.get("/api/queue/doctor/count/").data, {"present": 1, "pending": 0})
        row = next(x for x in self.client.get("/api/queue/doctor/").data["tickets"] if x["number"] == 1)
        self.assertEqual(row["arrived_at"], "2026-11-02T09:00+05:00")
        self.assertEqual(self.client.post("/api/queue/doctor/next/").data["skipped"], [])

    def test_count_only_for_doctor(self):
        self.client.force_authenticate(self.pat_user)
        self.assertEqual(self.client.get("/api/queue/doctor/count/").status_code, 403)


@override_settings(QUEUE_KIOSK_KEY="kiosk-secret", QUEUE_CHECKIN_WINDOW=45, FRONTEND_URL="https://medsecure.uz")
class QrCheckinTests(QueueTestBase):
    """Bemor kelganini faqat shifoxonadagi QR kod orqali tasdiqlaydi."""

    def kiosk_code(self, base=""):
        self.client.force_authenticate(None)
        r = self.client.get("/api/queue/kiosk/checkin-code/", {"base": base}, HTTP_X_KIOSK_KEY="kiosk-secret")
        self.assertEqual(r.status_code, 200)
        return r.data

    def checkin(self, code, user=None):
        self.client.force_authenticate(user or self.pat_user)
        return self.client.post("/api/queue/checkin/", {"code": code}, format="json")

    def code_of(self, data):
        return data["url"].split("?c=")[1]

    def test_kiosk_code_marks_all_today_tickets(self):
        a = self.take().data
        b = self.take(doctor=self.doctor2).data
        data = self.kiosk_code("https://medsecure.uz")
        self.assertTrue(data["url"].startswith("https://medsecure.uz/arrive?c=r."))
        self.assertTrue(data["svg"].startswith("<svg"))
        self.assertTrue(0 < data["expires_in"] <= 45)
        r = self.checkin(self.code_of(data))
        self.assertEqual((r.status_code, r.data["confirmed"]), (200, 2))
        self.assertTrue(QueueTicket.objects.get(pk=a["id"]).arrived_at)
        self.assertTrue(QueueTicket.objects.get(pk=b["id"]).arrived_at)
        self.assertEqual(self.checkin(self.code_of(data)).data["confirmed"], 0)   # takror - zararsiz
        self.assertTrue(AuditLog.objects.filter(actor=self.pat_user, detail__contains="QR orqali").exists())

    def test_code_expires(self):
        self.take()
        with mock.patch("appointments.checkin.time.time", return_value=1_000_000.0):
            code = self.code_of(self.kiosk_code())
        with mock.patch("appointments.checkin.time.time", return_value=1_000_000.0 + 50):
            self.assertEqual(self.checkin(code).status_code, 200)   # oldingi oyna hali amal qiladi
        t2 = self.take(self.pat2_user).data
        with mock.patch("appointments.checkin.time.time", return_value=1_000_000.0 + 100):
            r = self.checkin(code, self.pat2_user)                   # uyga yuborilgan eski rasm
        self.assertEqual((r.status_code, r.data["code"]), (400, "bad_code"))
        self.assertIsNone(QueueTicket.objects.get(pk=t2["id"]).arrived_at)

    def test_forged_and_foreign_codes_rejected(self):
        self.take()
        good = self.code_of(self.kiosk_code())
        w = good.split(".")[1]
        for bad in ["", "r.123.abc", f"r.{w}.{'0' * 20}", f"r.{int(w) + 5}.{good.split('.')[2]}", "p.nope", "x"]:
            self.assertEqual(self.checkin(bad).status_code, 400, bad)

    def test_no_ticket_and_roles(self):
        code = self.code_of(self.kiosk_code())
        self.assertEqual(self.checkin(code).data["code"], "no_ticket")
        self.assertEqual(self.checkin(code, self.doctor).status_code, 403)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get("/api/queue/kiosk/checkin-code/").status_code, 403)   # kalitsiz

    def test_qr_base_cannot_point_elsewhere(self):
        self.assertTrue(self.kiosk_code("https://evil.example")["url"].startswith("https://medsecure.uz/arrive"))

    def test_printed_code_lifecycle(self):
        self.take()
        self.client.force_authenticate(self.pat_user)
        self.assertEqual(self.client.post("/api/queue/admin/printed-checkin/").status_code, 403)
        self.client.force_authenticate(self.admin)
        self.assertFalse(self.client.get("/api/queue/admin/printed-checkin/").data["active"])
        first = self.code_of(self.client.post("/api/queue/admin/printed-checkin/").data)
        second = self.code_of(self.client.post("/api/queue/admin/printed-checkin/").data)
        self.assertNotEqual(first, second)
        self.assertEqual(self.checkin(first).status_code, 400)       # qayta yaratilgach eskisi yaroqsiz
        self.client.force_authenticate(self.admin)
        self.client.delete("/api/queue/admin/printed-checkin/")
        self.assertEqual(self.checkin(second).status_code, 400)      # bekor qilingan
        self.client.force_authenticate(self.admin)
        third = self.code_of(self.client.post("/api/queue/admin/printed-checkin/").data)
        self.assertEqual(self.checkin(third).status_code, 200)
        self.set_time(10, 0)
        self.now.return_value = at(10, 0) + timedelta(days=1)       # ertasi kun
        self.take()
        self.assertEqual(self.checkin(third).status_code, 400)

    @override_settings(QUEUE_CHECKIN_QR_REQUIRED=False)
    def test_button_can_be_reenabled_by_setting(self):
        t = self.take().data
        self.client.force_authenticate(self.pat_user)
        self.assertEqual(self.client.post(f"/api/queue/tickets/{t['id']}/arrive/").status_code, 200)


class QueueStatsTests(QueueTestBase):
    """Administrator statistikasi: kutish vaqti, kelmaganlar ulushi, shifokorlar reytingi."""

    def test_stats(self):
        self.set_time(8, 30)
        online = self.take().data                                   # №1 onlayn, kelmaydi
        self.set_time(9, 0)
        a = self.registry("A").data                                 # №2 09:00 keldi
        self.set_time(9, 10)
        b = self.registry("B").data                                 # №3 09:10 keldi
        self.registry("C", doctor=self.doctor2)                     # boshqa shifokor, ochiq qoladi
        self.client.force_authenticate(self.doctor)
        self.set_time(9, 20); self.client.post("/api/queue/doctor/next/")       # A: kutish 20
        self.set_time(9, 35); self.client.post("/api/queue/doctor/next/")       # A yakun (15), B: kutish 25
        self.set_time(9, 50); self.client.post("/api/queue/doctor/finish/")     # B yakun (15)
        self.client.post(f"/api/queue/doctor/tickets/{online['id']}/no-show/")
        self.client.force_authenticate(self.doctor)
        self.assertEqual(self.client.get("/api/queue/admin/stats/").status_code, 403)
        self.client.force_authenticate(self.admin)
        d = self.client.get("/api/queue/admin/stats/", {"days": 7}).data
        t = d["totals"]
        self.assertEqual((t["tickets"], t["done"], t["no_show"], t["open"]), (4, 2, 1, 1))
        self.assertEqual(t["no_show_rate"], 33.3)                   # 1 / (2 + 1)
        self.assertEqual((t["avg_wait_min"], t["median_wait_min"], t["avg_service_min"]), (22.5, 22.5, 15.0))
        self.assertEqual(d["doctors"][0]["id"], self.doctor.id)     # eng ko'p qabul
        self.assertEqual(d["doctors"][0]["no_show_rate"], 33.3)
        src = {x["source"]: x for x in d["by_source"]}
        self.assertEqual((src["online"]["no_show_rate"], src["registry"]["no_show_rate"]), (100.0, 0.0))
        self.assertEqual(len(d["by_day"]), 7)
        self.assertEqual(d["by_day"][-1]["done"], 2)
        self.assertEqual({h["hour"]: h["count"] for h in d["by_hour"]}, {9: 3})
        self.assertEqual(d["period"]["days"], 7)
        self.assertEqual(self.client.get("/api/queue/admin/stats/", {"days": "abc"}).data["period"]["days"], 30)
        self.assertEqual(self.client.get("/api/queue/admin/stats/", {"days": 5000}).data["period"]["days"], 365)



class RegistryIdentifyTests(QueueTestBase):
    """Registratura odamni bemor kartasi bilan aniqlaydi: busiz chipta berilmaydi."""

    def setUp(self):
        super().setUp()
        self.anvar_u = User.objects.create_user(username="anvar", password="x", role=Role.PATIENT,
                                                first_name="Anvar", last_name="Karimov", phone="+998901234567")
        self.anvar = Patient.objects.get(user=self.anvar_u)
        self.anvar.date_of_birth = datetime(1990, 5, 1).date()
        self.anvar.save()
        self.client.force_authenticate(self.admin)

    def test_ticket_requires_patient(self):
        r = self.client.post("/api/queue/registry/", {"doctor": self.doctor.id, "visitor_name": "Ali"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("patient", r.data)
        self.assertEqual(self.client.post("/api/queue/registry/", {"doctor": self.doctor.id, "patient": 99999},
                                          format="json").status_code, 400)

    def test_ticket_linked_and_doctor_gets_access(self):
        r = self.registry(patient=self.anvar)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.data["patient_name"], "Anvar Karimov")
        t = QueueTicket.objects.get(pk=r.data["id"])
        self.assertEqual((t.patient_id, t.arrived), (self.anvar.id, True))
        self.assertTrue(AuditLog.objects.filter(actor=self.admin, patient=self.anvar,
                                                detail__startswith="Registratura navbati").exists())
        self.client.force_authenticate(self.doctor)                    # bugungi navbat -> karta ochiq
        self.assertEqual(self.client.get(f"/api/records/patients/{self.anvar.id}/").status_code, 200)
        row = self.client.get("/api/queue/doctor/").data["tickets"][0]
        self.assertEqual((row["patient_id"], row["name"]), (self.anvar.id, "Anvar Karimov"))
        self.client.force_authenticate(self.doctor2)                   # boshqa shifokorga emas
        self.assertEqual(self.client.get(f"/api/records/patients/{self.anvar.id}/").status_code, 403)

    def test_duplicate_ticket_same_doctor(self):
        self.registry(patient=self.anvar)
        r = self.registry(patient=self.anvar)
        self.assertEqual((r.status_code, r.data["code"]), (409, "duplicate"))

    def test_search(self):
        def q(**params):
            return [p["id"] for p in self.client.get("/api/queue/registry/patients/", params).data]
        self.assertEqual(q(q="karimov anvar"), [self.anvar.id])
        self.assertEqual(q(q="anv", dob="1990-05-01"), [self.anvar.id])
        self.assertEqual(q(q="anv", dob="1991-05-01"), [])
        self.assertEqual(q(q="90 123 45 67".replace(" ", "")), [self.anvar.id])     # telefon bo'yicha
        self.assertEqual(q(), [])                                                    # bo'sh so'rov - hech narsa
        self.assertEqual(self.client.get("/api/queue/registry/patients/", {"dob": "xx"}).status_code, 400)
        self.client.force_authenticate(self.doctor)
        self.assertEqual(self.client.get("/api/queue/registry/patients/", {"q": "anvar"}).status_code, 403)

    def test_create_patient_and_duplicate_warning(self):
        body = {"first_name": " Dilnoza ", "last_name": "Usmonova", "date_of_birth": "1995-03-08", "phone": "+998 90 111-22-33"}
        r = self.client.post("/api/queue/registry/patients/", body, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual((r.data["full_name"], r.data["has_login"]), ("Dilnoza Usmonova", False))
        u = User.objects.get(username=r.data["username"])
        self.assertEqual((u.role, u.has_usable_password()), (Role.PATIENT, False))
        self.assertEqual(str(Patient.objects.get(user=u).date_of_birth), "1995-03-08")
        r2 = self.client.post("/api/queue/registry/patients/", body, format="json")
        self.assertEqual((r2.status_code, r2.data["code"]), (409, "possible_duplicate"))
        self.assertEqual(r2.data["matches"][0]["id"], r.data["id"])
        r3 = self.client.post("/api/queue/registry/patients/", {**body, "confirm": True}, format="json")
        self.assertEqual(r3.status_code, 201)
        bad = self.client.post("/api/queue/registry/patients/", {**body, "date_of_birth": "2999-01-01", "phone": "abc"}, format="json")
        self.assertEqual(set(bad.data), {"date_of_birth", "phone"})
        self.assertTrue(AuditLog.objects.filter(actor=self.admin, action="create",
                                                detail="Registraturada yangi bemor kartasi ochildi").exists())

    def test_link_kiosk_ticket(self):
        from .services import issue_ticket
        kiosk = issue_ticket(self.profile, QueueTicket.SOURCE_KIOSK)
        url = f"/api/queue/registry/tickets/{kiosk.id}/link/"
        self.client.force_authenticate(self.doctor)                    # shifokor bog'lay olmaydi
        self.assertEqual(self.client.post(url, {"patient": self.anvar.id, "no_photo_reason": NO_PHOTO}, format="json").status_code, 403)
        self.client.force_authenticate(self.admin)
        r = self.client.post(url, {"patient": self.anvar.id, "no_photo_reason": NO_PHOTO}, format="json")
        self.assertEqual((r.status_code, r.data["patient_id"], r.data["name"]), (200, self.anvar.id, "Anvar Karimov"))
        self.assertTrue(AuditLog.objects.filter(actor=self.admin, patient=self.anvar,
                                                detail__startswith="Navbat chiptasi bemorga bog'landi").exists())
        r = self.client.post(url, {"patient": self.patient.id, "no_photo_reason": NO_PHOTO}, format="json")          # qayta bog'lab bo'lmaydi
        self.assertEqual((r.status_code, r.data["code"]), (409, "already_linked"))
        other = issue_ticket(self.profile, QueueTicket.SOURCE_KIOSK)
        r = self.client.post(f"/api/queue/registry/tickets/{other.id}/link/", {"patient": self.anvar.id, "no_photo_reason": NO_PHOTO}, format="json")
        self.assertEqual((r.status_code, r.data["code"]), (409, "duplicate"))
        other.status = QueueTicket.CANCELLED
        other.save()
        self.assertEqual(self.client.post(f"/api/queue/registry/tickets/{other.id}/link/",
                                          {"patient": self.patient.id, "no_photo_reason": NO_PHOTO}, format="json").status_code, 404)
        self.client.force_authenticate(self.doctor)
        self.assertEqual(self.client.get(f"/api/records/patients/{self.anvar.id}/").status_code, 200)


@mock.patch("rest_framework.throttling.ScopedRateThrottle.allow_request", return_value=True)
class RegistryTempPasswordTests(QueueTestBase):
    """Registratura bemorga vaqtinchalik parol beradi; bemor birinchi kirishda o'z parolini o'rnatadi."""

    NEW_PW = "Qx7#vTm2!pLz9w"

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.admin)
        r = self.client.post("/api/queue/registry/patients/",
                             {"first_name": "Dilnoza", "last_name": "Usmonova", "date_of_birth": "1995-03-08"},
                             format="json")
        self.pid, self.username = r.data["id"], r.data["username"]

    def issue(self, pid=None):
        self.client.force_authenticate(self.admin)
        return self.client.post(f"/api/queue/registry/patients/{pid or self.pid}/temp-password/")

    def login(self, password, username=None):
        self.client.force_authenticate(None)
        return self.client.post("/api/auth/login/", {"username": username or self.username, "password": password},
                                format="json")

    def set_password(self, token, pw):
        return self.client.post("/api/auth/login/set-password/", {"pw_token": token, "new_password": pw},
                                format="json", HTTP_ACCEPT_LANGUAGE="uz")

    def test_issue_shows_password_once_and_audits(self, _):
        r = self.issue()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Cache-Control"], "no-store")
        pw = r.data["temp_password"]
        self.assertRegex(pw, r"^[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}$")
        self.assertEqual((r.data["username"], r.data["hours"]), (self.username, 72))
        u = User.objects.get(username=self.username)
        self.assertNotIn(pw, u.password)                               # faqat xesh
        self.assertTrue(u.must_change_password)
        log = AuditLog.objects.get(actor=self.admin, detail__startswith="Vaqtinchalik parol berildi")
        self.assertNotIn(pw, log.detail)
        row = self.client.get("/api/queue/registry/patients/", {"q": "Usmonova"}).data[0]
        self.assertFalse(row["has_login"])
        self.assertIsNotNone(row["temp_password_expires"])

    def test_patient_card_shows_login_state_to_admin_only(self, _):
        url = f"/api/records/patients/{self.pid}/"
        self.assertEqual(self.client.get(url).data["login"], {"has_login": False, "temp_password_expires": None})
        self.issue()
        self.assertIsNotNone(self.client.get(url).data["login"]["temp_password_expires"])
        pu = User.objects.get(username=self.username)
        self.client.force_authenticate(pu)
        self.assertIsNone(self.client.get(url).data["login"])

    def test_only_admin_and_only_patients(self, _):
        self.client.force_authenticate(self.doctor)
        self.assertEqual(self.client.post(f"/api/queue/registry/patients/{self.pid}/temp-password/").status_code, 403)
        self.assertEqual(self.issue(pid=99999).status_code, 404)
        # Shifokor hisobiga Patient yozuvi qo'lda bog'langan bo'lsa ham - rad etiladi
        from records.models import Patient
        p = Patient.objects.get(pk=self.pid)
        p.user.role = Role.DOCTOR
        p.user.save(update_fields=["role"])
        self.assertEqual(self.issue().status_code, 404)

    def test_temp_login_gives_no_tokens_until_password_set(self, _):
        pw = self.issue().data["temp_password"]
        r = self.login(pw)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data["password_change_required"])
        self.assertNotIn("access", r.data)
        self.assertNotIn("sm_refresh", r.cookies)
        token = r.data["pw_token"]
        weak = self.set_password(token, "123")
        self.assertEqual(weak.status_code, 400)
        self.assertIn("Parol faqat raqamlardan iborat bo'lmasligi kerak.", weak.data["detail"])   # interfeys tilida
        same = self.set_password(token, pw)
        self.assertIn("farq qilishi", same.data["detail"][0])
        ok = self.set_password(token, self.NEW_PW)
        self.assertEqual(ok.status_code, 200, ok.data)
        self.assertIn("access", ok.data)
        self.assertTrue(ok.cookies["sm_refresh"]["httponly"])
        self.assertEqual(self.set_password(token, "Zz9!another-pass").status_code, 401)   # bir martalik
        self.assertEqual(self.login(pw).status_code, 401)                                 # eski parol o'ldi
        r = self.login(self.NEW_PW)
        self.assertIn("access", r.data)
        u = User.objects.get(username=self.username)
        self.assertEqual((u.must_change_password, u.temp_password_expires), (False, None))

    def test_expired_temp_password(self, _):
        pw = self.issue().data["temp_password"]
        self.now.return_value = at(10, 0) + timedelta(hours=73)
        r = self.login(pw)
        self.assertEqual(r.status_code, 401)
        self.assertIn("registraturaga", str(r.data["detail"]))

    def test_reissue_revokes_old_password_and_sessions(self, _):
        from users.models import LoginSession
        pw = self.issue().data["temp_password"]
        token = self.login(pw).data["pw_token"]
        self.set_password(token, self.NEW_PW)
        self.assertEqual(LoginSession.objects.filter(user__username=self.username, revoked_at__isnull=True).count(), 1)
        r = self.issue()                                             # parolini unutdi
        self.assertIn("oldingi parol bekor qilindi", AuditLog.objects.filter(
            detail__startswith="Vaqtinchalik parol").order_by("-id").first().detail)
        self.assertEqual(LoginSession.objects.filter(user__username=self.username, revoked_at__isnull=True).count(), 0)
        self.assertEqual(self.login(self.NEW_PW).status_code, 401)    # unutilgan eski parol endi ishlamaydi
        self.assertTrue(self.login(r.data["temp_password"]).data["password_change_required"])

    def test_token_tampering_rejected(self, _):
        pw = self.issue().data["temp_password"]
        token = self.login(pw).data["pw_token"]
        self.assertEqual(self.set_password(token + "x", self.NEW_PW).status_code, 401)
        self.issue()                                                 # yangi parol -> eski token yaroqsiz
        self.assertEqual(self.set_password(token, self.NEW_PW).status_code, 401)

    def test_2fa_patient_still_needs_code(self, _):
        from users import mfa
        from users.models import TwoFactorDevice
        u = User.objects.get(username=self.username)
        TwoFactorDevice.objects.create(user=u, secret=mfa.new_secret(), confirmed_at=at(9, 0))
        u.is_active_2fa = True
        u.save()
        pw = self.issue().data["temp_password"]
        r = self.set_password(self.login(pw).data["pw_token"], self.NEW_PW)
        self.assertTrue(r.data["mfa_required"])
        self.assertNotIn("access", r.data)


@override_settings(QUEUE_KIOSK_KEY="")
class KioskDeviceTests(QueueTestBase):
    """Kiosk kalitlari administrator panelidan: alohida kalit, bir marta ko'rsatiladi, darhol bekor qilinadi."""

    def create(self, name="Kirish zali"):
        self.client.force_authenticate(self.admin)
        return self.client.post("/api/queue/kiosks/", {"name": name}, format="json")

    def kiosk_get(self, key):
        self.client.force_authenticate(None)
        return self.client.get("/api/queue/kiosk/doctors/", HTTP_X_KIOSK_KEY=key)

    def test_create_use_and_revoke(self):
        from .models import KioskDevice
        r = self.create()
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r["Cache-Control"], "no-store")
        key = r.data["key"]
        self.assertTrue(key.startswith("ks_") and len(key) > 30)
        d = KioskDevice.objects.get()
        self.assertNotIn(key, (d.key_hash, d.key_prefix))                # kalitning o'zi saqlanmaydi
        self.assertEqual(self.kiosk_get(key).status_code, 200)
        d.refresh_from_db()
        self.assertIsNotNone(d.last_used_at)
        listing = self.client.force_authenticate(self.admin) or self.client.get("/api/queue/kiosks/").data
        self.assertNotIn("key", listing["results"][0])                   # ro'yxatda kalit yo'q
        self.assertFalse(listing["legacy_key_active"])
        self.assertEqual(self.client.post(f"/api/queue/kiosks/{d.id}/revoke/").status_code, 200)
        self.assertEqual(self.kiosk_get(key).status_code, 403)           # darhol
        self.assertTrue(AuditLog.objects.filter(actor=self.admin, detail__startswith="Kiosk kaliti yaratildi").exists())
        self.assertTrue(AuditLog.objects.filter(actor=self.admin, detail__startswith="Kiosk kaliti bekor").exists())

    def test_keys_are_independent(self):
        k1, k2 = self.create("A").data["key"], self.create("B").data["key"]
        from .models import KioskDevice
        self.client.post(f"/api/queue/kiosks/{KioskDevice.objects.get(name='A').id}/revoke/")
        self.assertEqual(self.kiosk_get(k1).status_code, 403)
        self.assertEqual(self.kiosk_get(k2).status_code, 200)

    def test_wrong_or_missing_key(self):
        self.create()
        for key in ("", "ks_wrong", "x" * 500):
            self.assertEqual(self.kiosk_get(key).status_code, 403)

    def test_only_admin(self):
        self.client.force_authenticate(self.doctor)
        self.assertEqual(self.client.get("/api/queue/kiosks/").status_code, 403)
        self.assertEqual(self.client.post("/api/queue/kiosks/", {"name": "x"}, format="json").status_code, 403)
        self.assertEqual(self.create(name="   ").status_code, 400)

    @override_settings(QUEUE_KIOSK_KEY="legacy-key")
    def test_legacy_env_key_still_works(self):
        self.assertEqual(self.kiosk_get("legacy-key").status_code, 200)
        self.client.force_authenticate(self.admin)
        self.assertTrue(self.client.get("/api/queue/kiosks/").data["legacy_key_active"])


class RegistryIdentityChecksTests(QueueTestBase):
    """S8: registraturada noto'g'ri bemorni tanlashning oldini olish - rasm va hujjat raqamining oxirgi 4 raqami."""

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.admin)
        self.p = self.walk_in_patient("Anvar")
        self.p.national_id = "AB 1234567"
        self.p.save()

    def add_photo(self, patient):
        from records.models import PatientPhoto
        PatientPhoto.objects.create(patient=patient, image="x", uploaded_by=self.admin, updated_at=at(9))

    def issue(self, **extra):
        return self.client.post("/api/queue/registry/", {"doctor": self.doctor.id, "patient": self.p.id, **extra},
                                format="json")

    def test_last4_stored_and_searchable(self):
        self.p.refresh_from_db()
        self.assertEqual(self.p.national_id_last4, "4567")
        found = self.client.get("/api/queue/registry/patients/", {"q": "4567"}).data
        self.assertEqual([x["id"] for x in found], [self.p.id])
        self.assertEqual(found[0]["id_last4"], "4567")
        self.assertEqual(self.client.get("/api/queue/registry/patients/", {"q": "1234"}).data, [])
        self.p.national_id = ""
        self.p.save(update_fields=["national_id"])
        self.p.refresh_from_db()
        self.assertEqual(self.p.national_id_last4, "")

    def test_registry_creates_with_document(self):
        r = self.client.post("/api/queue/registry/patients/", {"first_name": "Dilnoza", "last_name": "Usmonova",
                             "date_of_birth": "1995-03-08", "national_id": "ad 7654321"}, format="json")
        self.assertEqual(r.data["id_last4"], "4321")
        from records.models import Patient
        self.assertEqual(Patient.objects.get(pk=r.data["id"]).national_id, "AD 7654321")

    def test_ticket_requires_photo_or_reason(self):
        r = self.issue()
        self.assertEqual((r.status_code, r.data["code"]), (409, "photo_required"))
        self.assertEqual(self.issue(no_photo_reason="yo'q").status_code, 409)      # juda qisqa sabab
        r = self.issue(no_photo_reason="Kamera ishlamayapti")
        self.assertEqual(r.status_code, 201)
        self.assertTrue(AuditLog.objects.filter(detail__endswith="rasmsiz: Kamera ishlamayapti").exists())

    def test_ticket_with_photo_needs_no_reason(self):
        self.add_photo(self.p)
        self.assertEqual(self.issue().status_code, 201)

    def test_link_kiosk_ticket_requires_photo_too(self):
        from .services import issue_ticket
        kiosk = issue_ticket(self.profile, QueueTicket.SOURCE_KIOSK)
        url = f"/api/queue/registry/tickets/{kiosk.id}/link/"
        r = self.client.post(url, {"patient": self.p.id}, format="json")
        self.assertEqual((r.status_code, r.data["code"]), (409, "photo_required"))
        self.add_photo(self.p)
        self.assertEqual(self.client.post(url, {"patient": self.p.id}, format="json").status_code, 200)
