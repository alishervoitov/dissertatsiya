"""
Laboratoriya natijalari va laborant roli testlari.
Ishlatish: python manage.py test labs --settings=config.settings_test
"""
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.utils import timezone
from rest_framework.test import APITestCase

from records.models import AuditLog, MedicalRecord, Patient, Prescription
from users.models import Role, User

from .models import LabResult, LabValue

PDF = b"%PDF-1.4\n%test\n"


class LabBase(APITestCase):
    def setUp(self):
        self.lab = User.objects.create_user(username="lab", password="x", role=Role.LAB, first_name="Laylo")
        self.lab2 = User.objects.create_user(username="lab2", password="x", role=Role.LAB)
        self.doctor = User.objects.create_user(username="doc", password="x", role=Role.DOCTOR)
        self.admin = User.objects.create_user(username="adm", password="x", role=Role.ADMIN)
        self.pat_user = User.objects.create_user(username="pat", password="x", role=Role.PATIENT, first_name="Anvar", last_name="Karimov")
        self.patient = Patient.objects.get(user=self.pat_user)
        self.pat2_user = User.objects.create_user(username="pat2", password="x", role=Role.PATIENT)
        self.patient2 = Patient.objects.get(user=self.pat2_user)

    def payload(self, patient=None, **extra):
        data = {
            "patient": (patient or self.patient).id, "panel": "cbc", "title": "Umumiy qon tahlili",
            "collected_at": timezone.now().isoformat(), "notes": "Och qoringa",
            "values": [
                {"name": "Gemoglobin", "value": "112", "unit": "g/l", "ref_low": "130", "ref_high": "160"},
                {"name": "Leykotsitlar", "value": "6,1", "unit": "10^9/l", "ref_low": "4", "ref_high": "9"},
                {"name": "Oqsil (siydik)", "value": "musbat", "ref_text": "manfiy"},
            ],
        }
        data.update(extra)
        return data

    def create(self, user=None, **kw):
        self.client.force_authenticate(user or self.lab)
        return self.client.post("/api/labs/results/", self.payload(**kw), format="json")


class LabResultTests(LabBase):
    def test_lab_creates_result_with_flags(self):
        r = self.create()
        self.assertEqual(r.status_code, 201, r.data)
        flags = [v["flag"] for v in r.data["values"]]
        self.assertEqual(flags, ["low", "normal", "abnormal"])
        self.assertEqual(r.data["abnormal_count"], 2)
        self.assertTrue(AuditLog.objects.filter(actor=self.lab, action="create", patient=self.patient).exists())

    def test_only_lab_can_create(self):
        for u in (self.doctor, self.admin, self.pat_user):
            self.assertEqual(self.create(user=u).status_code, 403)

    def test_values_encrypted_at_rest(self):
        r = self.create()
        vid = r.data["values"][0]["id"]
        with connection.cursor() as c:
            c.execute("SELECT value FROM labs_labvalue WHERE id=%s", [vid])
            raw = c.fetchone()[0]
        self.assertNotIn("112", raw)
        self.assertEqual(LabValue.objects.get(pk=vid).value, "112")

    def test_patient_sees_only_own(self):
        self.create()
        self.create(patient=self.patient2)
        self.client.force_authenticate(self.pat_user)
        rows = self.client.get("/api/labs/results/").data
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["patient"], self.patient.id)
        other = LabResult.objects.get(patient=self.patient2)
        self.assertEqual(self.client.get(f"/api/labs/results/{other.id}/").status_code, 404)

    def test_doctor_reads_by_patient(self):
        self.create()
        self.client.force_authenticate(self.doctor)
        self.assertEqual(self.client.get("/api/labs/results/").status_code, 400)
        rows = self.client.get(f"/api/labs/results/?patient={self.patient.id}").data
        self.assertEqual(len(rows), 1)
        self.assertTrue(AuditLog.objects.filter(actor=self.doctor, action="view", patient=self.patient).exists())

    def test_only_author_lab_edits_or_deletes(self):
        rid = self.create().data["id"]
        url = f"/api/labs/results/{rid}/"
        self.client.force_authenticate(self.lab2)
        self.assertEqual(self.client.patch(url, {"title": "X"}, format="json").status_code, 403)
        self.assertEqual(self.client.delete(url).status_code, 403)
        self.client.force_authenticate(self.doctor)
        self.assertEqual(self.client.patch(url, {"title": "X"}, format="json").status_code, 403)
        self.client.force_authenticate(self.lab)
        r = self.client.patch(url, {"values": [{"name": "Gemoglobin", "value": "140", "ref_low": "130", "ref_high": "160"}]}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([v["flag"] for v in r.data["values"]], ["normal"])
        self.assertEqual(self.client.delete(url).status_code, 204)

    def test_cannot_move_result_to_other_patient(self):
        rid = self.create().data["id"]
        self.client.force_authenticate(self.lab)
        r = self.client.patch(f"/api/labs/results/{rid}/", {"patient": self.patient2.id}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_validation(self):
        self.assertEqual(self.create(values=[]).status_code, 400)
        future = (timezone.now() + timezone.timedelta(days=1)).isoformat()
        self.assertEqual(self.create(collected_at=future).status_code, 400)
        bad = [{"name": "X", "value": "1", "ref_low": "10", "ref_high": "5"}]
        self.assertEqual(self.create(values=bad).status_code, 400)


class LabPatientSearchTests(LabBase):
    def test_search_minimum_and_fields(self):
        self.client.force_authenticate(self.lab)
        self.assertEqual(self.client.get("/api/labs/patients/?search=a").data, [])
        rows = self.client.get("/api/labs/patients/?search=Anvar Karimov").data
        self.assertEqual([r["id"] for r in rows], [self.patient.id])
        self.assertEqual(set(rows[0]), {"id", "full_name", "username", "date_of_birth", "gender"})

    def test_search_only_lab(self):
        self.client.force_authenticate(self.doctor)
        self.assertEqual(self.client.get("/api/labs/patients/?search=Anvar").status_code, 403)


class LabAttachmentTests(LabBase):
    def upload(self, rid, content, name="blank.pdf", ctype="application/pdf", user=None):
        self.client.force_authenticate(user or self.lab)
        f = SimpleUploadedFile(name, content, content_type=ctype)
        return self.client.post(f"/api/labs/results/{rid}/attachments/", {"file": f}, format="multipart")

    def test_upload_and_download_permissions(self):
        rid = self.create().data["id"]
        r = self.upload(rid, PDF)
        self.assertEqual(r.status_code, 201, r.data)
        aid = r.data["id"]
        self.client.force_authenticate(self.pat_user)
        self.assertEqual(self.client.get(f"/api/labs/attachments/{aid}/").status_code, 200)
        self.client.force_authenticate(self.pat2_user)
        self.assertEqual(self.client.get(f"/api/labs/attachments/{aid}/").status_code, 404)

    def test_spoofed_content_type_rejected(self):
        rid = self.create().data["id"]
        r = self.upload(rid, b"<script>alert(1)</script>", name="x.pdf", ctype="application/pdf")
        self.assertEqual(r.status_code, 400)

    def test_other_lab_cannot_attach(self):
        rid = self.create().data["id"]
        self.assertEqual(self.upload(rid, PDF, user=self.lab2).status_code, 403)


class LabRoleIsolationTests(LabBase):
    """Laborant klinik ma'lumotlarga (kasallik tarixi, retsept, PDF, fayllar) kira olmaydi."""

    def setUp(self):
        super().setUp()
        self.record = MedicalRecord.objects.create(
            patient=self.patient, created_by=self.doctor, title="Tashrif", diagnosis="Maxfiy tashxis",
            visit_date=timezone.now(),
        )
        Prescription.objects.create(
            patient=self.patient, prescribed_by=self.doctor, medication_name="Dori", dosage="1",
            duration_days=5, start_date=timezone.now().date(),
        )
        self.client.force_authenticate(self.lab)

    def test_no_medical_records(self):
        r = self.client.get("/api/records/medical-records/")
        self.assertIn(r.status_code, (200, 403))
        if r.status_code == 200:
            self.assertEqual(r.data, [])
        self.assertIn(self.client.get(f"/api/records/medical-records/{self.record.id}/").status_code, (403, 404))

    def test_no_prescriptions(self):
        r = self.client.get("/api/records/prescriptions/")
        self.assertIn(r.status_code, (200, 403))
        if r.status_code == 200:
            self.assertEqual(r.data, [])

    def test_no_patient_profile_or_list(self):
        self.assertEqual(self.client.get("/api/records/patients/").status_code, 403)
        self.assertIn(self.client.get(f"/api/records/patients/{self.patient.id}/").status_code, (403, 404))

    def test_no_history_pdf(self):
        self.assertEqual(self.client.get(f"/api/records/patients/{self.patient.id}/history-pdf/").status_code, 403)

    def test_not_a_doctor_for_queue(self):
        self.assertEqual(self.client.get("/api/queue/doctor/").status_code, 403)
        self.assertEqual(self.client.get("/api/queue/registry/").status_code, 403)


class ValidityAndPdfTests(LabBase):
    def make(self, panel, title, days_ago, valid_days=None):
        from datetime import timedelta
        collected = timezone.now() - timedelta(days=days_ago)
        r = LabResult.objects.create(
            patient=self.patient, panel=panel, title=title, collected_at=collected, created_by=self.lab,
            valid_until=(collected.date() + timedelta(days=valid_days)) if valid_days is not None else None,
        )
        LabValue.objects.create(result=r, name="Gemoglobin", value="112", unit="g/l", ref_low=130, ref_high=160)
        return r

    def test_default_validity_set_on_create(self):
        r = self.create(panel="urine")
        from datetime import date
        collected = date.fromisoformat(r.data["collected_at"][:10])
        self.assertEqual((date.fromisoformat(r.data["valid_until"]) - collected).days, 10)
        self.assertFalse(r.data["is_expired"])

    def test_valid_until_before_collection_rejected(self):
        r = self.create(valid_until="2000-01-01")
        self.assertEqual(r.status_code, 400)
        self.assertIn("valid_until", r.data)

    def test_latest_per_type_and_expiry(self):
        from .services import latest_valid_results
        self.make("cbc", "Qon 1", days_ago=6, valid_days=10)
        cbc_new = self.make("cbc", "Qon 2", days_ago=1, valid_days=10)
        self.make("urine", "Siydik", days_ago=20, valid_days=10)          # muddati o'tgan
        hormone = self.make("custom", "Gormonlar", days_ago=2, valid_days=30)
        self.make("custom", "Vitamin D", days_ago=3, valid_days=30)
        got = {r.title for r in latest_valid_results(self.patient)}
        self.assertEqual(got, {"Qon 2", "Gormonlar", "Vitamin D"})
        self.assertIn(cbc_new.title, got)

    def test_expired_latest_does_not_fall_back_to_older(self):
        from .services import latest_valid_results
        self.make("biochem", "Eski", days_ago=40, valid_days=60)
        self.make("biochem", "Yangi", days_ago=20, valid_days=14)        # eng oxirgisi, lekin muddati o'tgan
        self.assertEqual(latest_valid_results(self.patient), [])

    def test_pdf_includes_valid_labs_and_skips_old_lab_records(self):
        from unittest import mock
        self.make("cbc", "Qon 2", days_ago=1, valid_days=10)
        self.make("urine", "Siydik", days_ago=20, valid_days=10)
        MedicalRecord.objects.create(patient=self.patient, created_by=self.doctor, record_type="lab_result",
                                     title="Qon tahlili", visit_date=timezone.now())
        MedicalRecord.objects.create(patient=self.patient, created_by=self.doctor, record_type="visit",
                                     title="Tashrif", visit_date=timezone.now())
        self.client.force_authenticate(self.pat_user)
        from records import views as rv
        with mock.patch.object(rv, "build_patient_history_pdf", wraps=rv.build_patient_history_pdf) as spy:
            r = self.client.get(f"/api/records/patients/{self.patient.id}/history-pdf/")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(b"".join(r.streaming_content).startswith(b"%PDF"))
        args, kwargs = spy.call_args
        self.assertEqual([x.title for x in args[1]], ["Tashrif"])
        self.assertEqual([x.title for x in kwargs["lab_results"]], ["Qon 2"])

    def test_pdf_renders_in_all_languages(self):
        self.make("cbc", "<b>Qon & 2", days_ago=1, valid_days=10)
        self.client.force_authenticate(self.pat_user)
        for lang in ("uz", "ru", "en"):
            r = self.client.get(f"/api/records/patients/{self.patient.id}/history-pdf/?lang={lang}")
            self.assertEqual(r.status_code, 200)
            self.assertTrue(b"".join(r.streaming_content).startswith(b"%PDF"))
