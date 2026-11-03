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


class MyAccessLogTests(APITestCase):
    """"Kartamni kim ko'rdi": bemor faqat o'z kartasiga bo'lgan murojaatlarni ko'radi."""

    def setUp(self):
        self.doctor = User.objects.create_user(username="acdoc", password="x", role=Role.DOCTOR, first_name="Sardor", last_name="Nazarov")
        self.lab = User.objects.create_user(username="aclab", password="x", role=Role.LAB)
        pu = User.objects.create_user(username="acpat", password="x", role=Role.PATIENT)
        self.pat_user = pu
        self.patient = Patient.objects.get(user=pu)
        pu2 = User.objects.create_user(username="acpat2", password="x", role=Role.PATIENT)
        self.patient2 = Patient.objects.get(user=pu2)
        AuditLog.objects.create(actor=self.doctor, action="view", patient=self.patient, detail="Bemor kartasi ko'rildi", ip_address="10.1.2.3")
        AuditLog.objects.create(actor=self.doctor, action="export", patient=self.patient, detail="PDF")
        AuditLog.objects.create(actor=self.lab, action="create", patient=self.patient, detail="Tahlil")
        AuditLog.objects.create(actor=pu, action="view", patient=self.patient, detail="O'z profilini ko'rdi")
        AuditLog.objects.create(actor=self.doctor, action="view", patient=self.patient2, detail="Boshqa bemor")
        old = AuditLog.objects.create(actor=self.doctor, action="view", patient=self.patient, detail="Eski")
        AuditLog.objects.filter(pk=old.pk).update(timestamp=timezone.now() - timedelta(days=100))

    def get(self, user=None, q=""):
        self.client.force_authenticate(user or self.pat_user)
        return self.client.get(f"/api/records/access-log/me/{q}")

    def test_only_own_card_and_not_own_actions(self):
        r = self.get()
        self.assertEqual(r.status_code, 200)
        details = [x["detail"] for x in r.data["results"]]
        self.assertNotIn("Boshqa bemor", details)
        self.assertNotIn("O'z profilini ko'rdi", details)
        self.assertEqual(r.data["summary"], {"people": 2, "events": 4})
        self.assertIn("Sardor Nazarov", [x["actor_name"] for x in r.data["results"]])
        self.assertEqual({x["actor_role"] for x in r.data["results"]}, {"doctor", "lab"})

    def test_no_ip_or_user_agent_exposed(self):
        row = self.get().data["results"][0]
        self.assertNotIn("ip_address", row)
        self.assertNotIn("user_agent", row)
        self.assertNotIn("10.1.2.3", str(self.get().data))

    def test_days_filter(self):
        details = [x["detail"] for x in self.get(q="?days=30").data["results"]]
        self.assertNotIn("Eski", details)
        self.assertEqual(len(details), 3)

    def test_only_patients(self):
        self.assertEqual(self.get(self.doctor).status_code, 403)
        self.assertEqual(self.get(self.lab).status_code, 403)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get("/api/records/access-log/me/").status_code, 401)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix="att-test-"))
class RecordAttachmentTypeTests(APITestCase):
    """Shifokor yozuviga biriktiriladigan fayl turi imzo bo'yicha tekshiriladi (Content-Type ga ishonilmaydi)."""

    def setUp(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        self.SUF = SimpleUploadedFile
        self.doctor = User.objects.create_user(username="atdoc", password="x", role=Role.DOCTOR)
        pu = User.objects.create_user(username="atpat", password="x", role=Role.PATIENT)
        self.record = MedicalRecord.objects.create(patient=Patient.objects.get(user=pu), created_by=self.doctor,
                                                   title="T", visit_date=timezone.now())
        self.client.force_authenticate(self.doctor)

    def upload(self, name, data, ctype):
        return self.client.post(f"/api/records/medical-records/{self.record.id}/attachments/",
                                {"file": self.SUF(name, data, content_type=ctype)}, format="multipart")

    def png(self):
        b = BytesIO(); Image.new("RGB", (4, 4)).save(b, "PNG"); return b.getvalue()

    def test_genuine_files_accepted(self):
        self.assertEqual(self.upload("a.pdf", b"%PDF-1.4\n%%EOF", "application/pdf").status_code, 201)
        self.assertEqual(self.upload("a.png", self.png(), "image/png").status_code, 201)

    def test_spoofed_files_rejected(self):
        cases = [
            ("x.pdf", b"<html><script>alert(1)</script></html>", "application/pdf"),
            ("x.png", b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', "image/png"),
            ("x.jpg", b"MZ\x90\x00" + b"\x00" * 60, "image/jpeg"),
            ("x.png", b"<?php system($_GET['c']); ?>", "image/png"),
            ("x.webp", b"RIFF\x24\x00\x00\x00WAVEfmt ", "image/webp"),
        ]
        for name, data, ctype in cases:
            self.assertEqual(self.upload(name, data, ctype).status_code, 400, name)
        self.assertEqual(self.record.attachments.count(), 0)

    def test_detected_type_stored_and_random_name(self):
        r = self.upload("../../evil name.html", self.png(), "image/jpeg")  # haqiqiy PNG, noto'g'ri sarlavha
        self.assertEqual(r.status_code, 201)
        from .models import RecordAttachment
        att = RecordAttachment.objects.get(pk=r.data["id"])
        self.assertEqual(att.content_type, "image/png")
        self.assertTrue(att.file.name.endswith(".png"))
        self.assertNotIn("evil", att.file.name)


class AuditHashChainTests(APITestCase):
    """Audit jurnali HMAC xesh zanjiri: har qanday o'zgartirish, o'chirish yoki qisqartirish aniqlanadi."""

    def setUp(self):
        self.admin = User.objects.create_user(username="aud_admin", password="x", role=Role.ADMIN)
        self.doctor = User.objects.create_user(username="aud_doc", password="x", role=Role.DOCTOR)
        self.patient = Patient.objects.get(user=User.objects.create_user(username="aud_pat", password="x", role=Role.PATIENT))
        from .audit import log_audit
        self.entries = [log_audit(self.doctor, "view", patient=self.patient, detail=f"Ko'rish {i}") for i in range(6)]

    def verify(self):
        from . import audit_chain
        from .models import AuditCheckpoint
        return audit_chain.verify(AuditLog, AuditCheckpoint)

    def test_chain_links_and_verifies(self):
        e = self.entries
        self.assertEqual(e[0].prev_hash, "0" * 64)
        for a, b in zip(e, e[1:]):
            self.assertEqual(b.prev_hash, a.entry_hash)
        self.assertEqual(e[2].actor_snapshot, f"{self.doctor.pk}:aud_doc:doctor")
        r = self.verify()
        self.assertTrue(r["ok"])
        self.assertEqual((r["checked"], r["head"]), (6, e[-1].entry_hash))

    def test_direct_create_is_also_chained(self):
        x = AuditLog.objects.create(actor=self.admin, action="view", detail="to'g'ridan-to'g'ri")
        self.assertEqual(x.prev_hash, self.entries[-1].entry_hash)
        self.assertTrue(self.verify()["ok"])

    def test_orm_save_and_delete_blocked(self):
        e = self.entries[1]
        e.detail = "soxta"
        with self.assertRaises(PermissionError):
            e.save()
        with self.assertRaises(PermissionError):
            e.delete()

    def test_detects_modified_detail(self):
        AuditLog.objects.filter(pk=self.entries[2].pk).update(detail="Hech narsa ko'rilmadi")
        r = self.verify()
        self.assertFalse(r["ok"])
        self.assertEqual(r["problems"], [{"type": "modified", "id": self.entries[2].pk}])

    def test_detects_changed_actor_or_time(self):
        AuditLog.objects.filter(pk=self.entries[3].pk).update(actor_snapshot=f"{self.admin.pk}:aud_admin:admin")
        AuditLog.objects.filter(pk=self.entries[4].pk).update(timestamp=timezone.now() - timedelta(days=3))
        ids = {p["id"] for p in self.verify()["problems"]}
        self.assertEqual(ids, {self.entries[3].pk, self.entries[4].pk})

    def test_detects_deleted_middle_row(self):
        AuditLog.objects.filter(pk=self.entries[2].pk)._raw_delete(AuditLog.objects.db)
        r = self.verify()
        self.assertEqual(r["problems"], [{"type": "link", "id": self.entries[3].pk}])

    def test_rehash_without_key_is_detected(self):
        """Buzg'unchi oddiy SHA-256 bilan zanjirni qayta hisoblasa ham HMAC kaliti yo'qligi sabab ushlanadi."""
        import hashlib
        from . import audit_chain
        e = AuditLog.objects.get(pk=self.entries[5].pk)
        e.detail = "yashirilgan"
        fake = hashlib.sha256(audit_chain.payload(e, e.prev_hash)).hexdigest()
        AuditLog.objects.filter(pk=e.pk).update(detail="yashirilgan", entry_hash=fake)
        self.assertEqual(self.verify()["problems"], [{"type": "modified", "id": e.pk}])

    def test_tail_truncation_detected_by_checkpoint(self):
        from .audit_integrity import run_check
        self.assertTrue(run_check(make_checkpoint=True)["ok"])
        AuditLog.objects.filter(pk__gte=self.entries[4].pk)._raw_delete(AuditLog.objects.db)
        r = self.verify()
        self.assertFalse(r["ok"])
        self.assertEqual(r["problems"][0]["type"], "truncated")

    def test_user_deletion_keeps_chain_valid(self):
        self.doctor.delete()                       # FK NULL bo'ladi, lekin snapshot xeshda saqlangan
        self.assertTrue(self.verify()["ok"])
        self.assertEqual(AuditLog.objects.get(pk=self.entries[0].pk).actor_snapshot.split(":")[1], "aud_doc")

    def test_api_and_alert(self):
        from monitoring.models import SecurityAlert
        self.client.force_authenticate(self.doctor)
        self.assertEqual(self.client.get("/api/records/audit-logs/verify/").status_code, 403)
        self.client.force_authenticate(self.admin)
        r = self.client.get("/api/records/audit-logs/verify/").data
        self.assertTrue(r["ok"])
        AuditLog.objects.filter(pk=self.entries[0].pk).update(detail="x")
        r = self.client.get("/api/records/audit-logs/verify/").data
        self.assertFalse(r["ok"])
        self.assertEqual(SecurityAlert.objects.filter(rule="audit_tamper", severity="high").count(), 1)
        self.client.get("/api/records/audit-logs/verify/")
        self.assertEqual(SecurityAlert.objects.get(rule="audit_tamper").count, 2)   # takroriy alert emas

    def test_daily_task_creates_checkpoint(self):
        from .models import AuditCheckpoint
        from .tasks import verify_audit_chain
        self.assertEqual(verify_audit_chain()["ok"], True)
        cp = AuditCheckpoint.objects.get()
        self.assertEqual((cp.last_id, cp.count), (self.entries[-1].pk, 6))


class AuditRekeyTests(TestCase):
    def setUp(self):
        from .audit import log_audit
        self.doctor = User.objects.create_user(username="rk_doc", password="x", role=Role.DOCTOR)
        for i in range(4):
            log_audit(self.doctor, "view", detail=f"r{i}")          # SECRET_KEY asosidagi kalit

    def test_rekey_from_secret_key(self):
        from django.core.management import call_command
        from . import audit_chain
        with override_settings(AUDIT_HMAC_KEY="d" * 64):
            self.assertFalse(audit_chain.verify(AuditLog)["ok"])   # yangi kalit bilan eski zanjir o'tmaydi
            call_command("rekey_audit_chain", "--from-secret-key")
            r = audit_chain.verify(AuditLog)
            self.assertTrue(r["ok"])
            self.assertEqual(r["checked"], 5)                        # + kalit almashtirilgani haqida yozuv

    def test_rekey_refuses_tampered_chain(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError
        AuditLog.objects.filter(detail="r1").update(detail="soxta")
        with override_settings(AUDIT_HMAC_KEY="d" * 64):
            with self.assertRaises(CommandError):
                call_command("rekey_audit_chain", "--from-secret-key")
