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

from .models import AuditLog, CareGrant, EmergencyAccess, MedicalRecord, Patient, Prescription
from .pdf_export import _text
from .tasks import deactivate_expired_prescriptions


from contextlib import contextmanager


@contextmanager
def attacker_bypass():
    """Hujumchini taqlid qilish: baza superfoydalanuvchisi triggerlarni chetlab o'tadi (STRIDE T1).

    PostgreSQL'da audit jurnalini UPDATE/DELETE trigger to'sadi - bu testlar aynan shu to'siqdan
    keyingi qatlamni (HMAC zanjiri buzilganini aniqlashni) tekshiradi. SQLite'da trigger yo'q."""
    from django.db import connection
    if connection.vendor != "postgresql":
        yield
        return
    with connection.cursor() as c:
        c.execute("SET session_replication_role = replica")
    try:
        yield
    finally:
        with connection.cursor() as c:
            c.execute("SET session_replication_role = DEFAULT")



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
        from unittest import mock

        from django_celery_beat.schedulers import DatabaseScheduler

        from config.celery import app

        # Rejalashtiruvchi close_old_connections() chaqiradi: Postgres'da u test tranzaksiyasi
        # ichidagi ulanishni yopib qo'yadi (SQLite'da sezilmaydi). Test uchun o'chiramiz.
        with mock.patch("django_celery_beat.schedulers.close_old_connections"):
            scheduler = DatabaseScheduler(app=app, lazy=False)
        task = PeriodicTask.objects.get(name="deactivate-expired-prescriptions")
        self.assertTrue(task.enabled)
        self.assertEqual((task.crontab.minute, task.crontab.hour), ("30", "0"))
        self.assertIsNone(task.last_run_at)
        with mock.patch("django_celery_beat.schedulers.close_old_connections"):
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
        CareGrant.objects.create(patient=self.patient, doctor=self.doctor)   # bemor roziligi
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
        with attacker_bypass():                  # vaqtni sun'iy eskirtirish (Postgres'da trigger to'sadi)
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
        CareGrant.objects.create(patient=self.record.patient, doctor=self.doctor)
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
        with attacker_bypass():
            AuditLog.objects.filter(pk=self.entries[2].pk).update(detail="Hech narsa ko'rilmadi")
        r = self.verify()
        self.assertFalse(r["ok"])
        self.assertEqual(r["problems"], [{"type": "modified", "id": self.entries[2].pk}])

    def test_detects_changed_actor_or_time(self):
        with attacker_bypass():
            AuditLog.objects.filter(pk=self.entries[3].pk).update(actor_snapshot=f"{self.admin.pk}:aud_admin:admin")
            AuditLog.objects.filter(pk=self.entries[4].pk).update(timestamp=timezone.now() - timedelta(days=3))
        ids = {p["id"] for p in self.verify()["problems"]}
        self.assertEqual(ids, {self.entries[3].pk, self.entries[4].pk})

    def test_detects_deleted_middle_row(self):
        with attacker_bypass():
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
        with attacker_bypass():
            AuditLog.objects.filter(pk=e.pk).update(detail="yashirilgan", entry_hash=fake)
        self.assertEqual(self.verify()["problems"], [{"type": "modified", "id": e.pk}])

    def test_tail_truncation_detected_by_checkpoint(self):
        from .audit_integrity import run_check
        self.assertTrue(run_check(make_checkpoint=True)["ok"])
        with attacker_bypass():
            AuditLog.objects.filter(pk__gte=self.entries[4].pk)._raw_delete(AuditLog.objects.db)
        r = self.verify()
        self.assertFalse(r["ok"])
        self.assertEqual(r["problems"][0]["type"], "truncated")

    def test_postgres_trigger_blocks_direct_sql(self):
        """Birinchi qatlam: Postgres'da ORM'ni chetlab o'tgan to'g'ridan-to'g'ri SQL ham rad etiladi."""

        from django.db import DatabaseError, connection, transaction
        if connection.vendor != "postgresql":
            self.skipTest("Trigger faqat PostgreSQL'da (CI'da tekshiriladi)")
        pk = self.entries[1].pk
        for sql in (f"UPDATE records_auditlog SET detail = 'x' WHERE id = {pk}",
                    f"DELETE FROM records_auditlog WHERE id = {pk}",
                    "TRUNCATE records_auditlog CASCADE"):
            with self.subTest(sql=sql.split()[0]):
                with self.assertRaises(DatabaseError), transaction.atomic(), connection.cursor() as c:
                    c.execute(sql)
        self.assertTrue(self.verify()["ok"])

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
        with attacker_bypass():
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


@override_settings(TELEGRAM_BOT_TOKEN="123:SECRET-TOKEN", TELEGRAM_AUDIT_CHAT_ID="42")
class AuditAnchorTests(TestCase):
    """T1: zanjir oxiri tashqi guvohga (Telegram) yuboriladi; kalit bilan qayta yozilgan zanjir ham ushlanadi."""

    def setUp(self):
        from .audit import log_audit
        self.doctor = User.objects.create_user(username="anc_doc", password="x", role=Role.DOCTOR)
        self.entries = [log_audit(self.doctor, "view", detail=f"Ko'rish {i}") for i in range(5)]

    def fake_urlopen(self, sent, ok=True):
        import json
        from unittest import mock

        def opener(req, timeout=None):
            sent.append((req.full_url, json.loads(req.data)))
            resp = mock.MagicMock()
            resp.__enter__.return_value.read.return_value = json.dumps(
                {"ok": True, "result": {"message_id": 777}} if ok else {"ok": False, "description": "chat not found"}
            ).encode()
            return resp
        return opener

    def test_daily_task_sends_anchor_without_patient_data(self):
        from unittest import mock
        from .models import AuditCheckpoint
        from .tasks import verify_audit_chain
        sent = []
        with mock.patch("monitoring.telegram.urllib.request.urlopen", self.fake_urlopen(sent)):
            self.assertTrue(verify_audit_chain()["ok"])
        cp = AuditCheckpoint.objects.get()
        self.assertEqual(len(sent), 1)
        url, body = sent[0]
        self.assertIn("/bot123:SECRET-TOKEN/sendMessage", url)
        self.assertEqual(body["chat_id"], "42")
        self.assertIn(cp.last_hash, body["text"])
        self.assertIn(f"verify_audit_anchor {cp.last_id} {cp.last_hash} --count 5", body["text"])
        self.assertNotIn("Ko'rish", body["text"])          # yozuv mazmuni chiqmaydi

    @override_settings(QUEUE_TIME_ZONE="Asia/Tashkent")
    def test_message_time_is_local(self):
        from datetime import datetime, timezone as dt_tz
        from .audit_anchor import build_message
        r = {"ok": True, "last_id": 1, "checked": 1, "head": "a" * 64,
             "checked_at": datetime(2026, 11, 7, 8, 33, tzinfo=dt_tz.utc)}
        self.assertIn("2026-11-07 13:33", build_message(r))

    def test_broken_chain_sends_alarm(self):
        from unittest import mock
        from .tasks import verify_audit_chain
        with attacker_bypass():
            AuditLog.objects.filter(pk=self.entries[1].pk).update(detail="x")
        sent = []
        with mock.patch("monitoring.telegram.urllib.request.urlopen", self.fake_urlopen(sent)):
            self.assertFalse(verify_audit_chain()["ok"])
        self.assertIn("BUZILGAN", sent[0][1]["text"])
        self.assertNotIn("verify_audit_anchor", sent[0][1]["text"])

    @override_settings(TELEGRAM_BOT_TOKEN="", TELEGRAM_AUDIT_CHAT_ID="")
    def test_not_configured_skips_quietly(self):
        from unittest import mock
        from .tasks import verify_audit_chain
        with mock.patch("monitoring.telegram.urllib.request.urlopen") as m:
            self.assertTrue(verify_audit_chain()["ok"])
        m.assert_not_called()

    def test_send_error_never_leaks_token(self):
        import urllib.error
        from unittest import mock
        from .audit_anchor import AnchorSendError, send
        for exc in (urllib.error.URLError("https://api.telegram.org/bot123:SECRET-TOKEN/x"), TimeoutError()):
            with mock.patch("monitoring.telegram.urllib.request.urlopen", side_effect=exc):
                with self.assertRaises(AnchorSendError) as cm:
                    send("x")
            self.assertNotIn("SECRET", str(cm.exception))
        with mock.patch("monitoring.telegram.urllib.request.urlopen", self.fake_urlopen([], ok=False)):
            with self.assertRaisesMessage(AnchorSendError, "chat not found"):
                send("x")

    def test_failed_send_is_retried_then_gives_up(self):
        from unittest import mock
        from .tasks import send_audit_anchor
        with mock.patch("monitoring.telegram.urllib.request.urlopen", side_effect=TimeoutError()) as m:
            send_audit_anchor.apply(args=["x"])
        self.assertEqual(m.call_count, send_audit_anchor.max_retries + 1)

    def anchor(self):
        from .audit_anchor import check_anchor
        last = self.entries[-1]
        return lambda **kw: check_anchor(AuditLog, last.pk, last.entry_hash, 5, **kw)

    def test_anchor_matches_untouched_chain(self):
        from .audit import log_audit
        check = self.anchor()
        log_audit(self.doctor, "view", detail="langardan keyin")   # keyingi yozuvlar langarga ta'sir qilmaydi
        self.assertTrue(check()["ok"])

    def test_rewritten_chain_with_key_is_caught_by_anchor(self):
        """Server egallangan: buzg'unchi kalitni biladi va zanjirni qayta hisoblaydi - ichki tekshiruv o'tadi,
        lekin Telegram'dagi langar mos kelmaydi."""
        from . import audit_chain
        from .models import AuditCheckpoint
        check = self.anchor()
        with attacker_bypass():
            AuditLog.objects.filter(pk=self.entries[1].pk).update(detail="Hech narsa ko'rilmadi")
            prev = audit_chain.GENESIS
            for e in AuditLog.objects.order_by("id"):
                h = audit_chain.compute_hash(e, prev)
                AuditLog.objects.filter(pk=e.pk).update(prev_hash=prev, entry_hash=h)
                prev = h
        self.assertTrue(audit_chain.verify(AuditLog, AuditCheckpoint)["ok"])   # ichki tekshiruv aldandi
        r = check()
        self.assertFalse(r["ok"])
        self.assertIn("qayta yozilgan", " ".join(r["problems"]))

    def test_deleted_rows_caught_by_anchor(self):
        check = self.anchor()
        with attacker_bypass():
            AuditLog.objects.filter(pk=self.entries[-1].pk)._raw_delete(AuditLog.objects.db)
        self.assertIn("o'chirilgan", " ".join(check()["problems"]))

    def test_command(self):
        from io import StringIO
        from django.core.management import call_command
        from django.core.management.base import CommandError
        last = self.entries[-1]
        out = StringIO()
        call_command("verify_audit_anchor", str(last.pk), last.entry_hash, "--count", "5", stdout=out)
        self.assertIn("Langar mos", out.getvalue())
        with self.assertRaises(SystemExit):
            call_command("verify_audit_anchor", str(last.pk), "0" * 64, stdout=StringIO())
        with self.assertRaises(CommandError):
            call_command("verify_audit_anchor", str(last.pk), "zz", stdout=StringIO())


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
        with attacker_bypass():
            AuditLog.objects.filter(detail="r1").update(detail="soxta")
        with override_settings(AUDIT_HMAC_KEY="d" * 64):
            with self.assertRaises(CommandError):
                call_command("rekey_audit_chain", "--from-secret-key")


class EncryptionKeyRotationTests(TestCase):
    """Fernet kalitini almashtirish: eski ma'lumot yo'qolmaydi, eski kalit keyin olib tashlanadi."""

    OLD = Fernet.generate_key().decode()
    NEW = Fernet.generate_key().decode()

    def raw(self, table, col, pk):
        from django.db import connection
        with connection.cursor() as c:
            c.execute(f"SELECT {col} FROM {table} WHERE id = %s", [pk])
            return c.fetchone()[0]

    def run_cmd(self, *args):
        from io import StringIO
        from django.core.management import call_command
        out = StringIO()
        call_command("rotate_encryption_key", *args, stdout=out)
        return out.getvalue()

    def test_full_rotation_cycle(self):
        with override_settings(FIELD_ENCRYPTION_KEY=self.OLD):
            u = User.objects.create_user(username="rot_pat", password="x", role=Role.PATIENT)
            p = Patient.objects.get(user=u)
            p.allergies, p.national_id = "Penitsillin", "AA1234567"
            p.save()
            doc = User.objects.create_user(username="rot_doc", password="x", role=Role.DOCTOR)
            rec = MedicalRecord.objects.create(patient=p, created_by=doc, title="Ko'rik", visit_date=timezone.now().date(), diagnosis="Gripp", treatment="Dam olish")

        with override_settings(FIELD_ENCRYPTION_KEY=f"{self.NEW},{self.OLD}"):
            self.assertEqual(Patient.objects.get(pk=p.pk).allergies, "Penitsillin")   # eski kalit bilan o'qiladi
            out = self.run_cmd("--check")
            self.assertIn("eski kalit #1:         4 ta", out)
            self.run_cmd()
            self.assertIn("eski kalit #1:         0 ta", self.run_cmd("--check"))

        # Eski kalit olib tashlangach ham hammasi o'qiladi
        with override_settings(FIELD_ENCRYPTION_KEY=self.NEW):
            p2 = Patient.objects.get(pk=p.pk)
            self.assertEqual((p2.allergies, p2.national_id), ("Penitsillin", "AA1234567"))
            r2 = MedicalRecord.objects.get(pk=rec.pk)
            self.assertEqual((r2.diagnosis, r2.treatment), ("Gripp", "Dam olish"))
        Fernet(self.NEW.encode()).decrypt(self.raw("records_patient", "allergies", p.pk).encode())
        with self.assertRaises(Exception):
            Fernet(self.OLD.encode()).decrypt(self.raw("records_patient", "allergies", p.pk).encode())

    def test_refuses_when_value_cannot_be_decrypted(self):
        from django.core.management.base import CommandError
        lost = Fernet.generate_key().decode()
        with override_settings(FIELD_ENCRYPTION_KEY=lost):
            u = User.objects.create_user(username="rot_lost", password="x", role=Role.PATIENT)
            p = Patient.objects.get(user=u)
            p.allergies = "Yong'oq"
            p.save()
        with override_settings(FIELD_ENCRYPTION_KEY=f"{self.NEW},{self.OLD}"):
            with self.assertRaises(CommandError):
                self.run_cmd()
        with override_settings(FIELD_ENCRYPTION_KEY=lost):                         # ma'lumot buzilmagan
            self.assertEqual(Patient.objects.get(pk=p.pk).allergies, "Yong'oq")

    def test_bad_key_format(self):
        from django.core.management.base import CommandError
        with override_settings(FIELD_ENCRYPTION_KEY=f"{self.NEW},notakey"):
            with self.assertRaises(CommandError):
                self.run_cmd("--check")



class PatientConsentTests(APITestCase):
    """Bemor roziligi: shifokor faqat ruxsat, bugungi navbat yoki favqulodda kirish bilan kartani ko'radi."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.doctor = User.objects.create_user(username="cdoc", password="x", role=Role.DOCTOR, first_name="Sardor")
        self.doctor2 = User.objects.create_user(username="cdoc2", password="x", role=Role.DOCTOR)
        self.pu = User.objects.create_user(username="cpat", password="x", role=Role.PATIENT, first_name="Anvar")
        self.patient = Patient.objects.get(user=self.pu)
        self.pu2 = User.objects.create_user(username="cpat2", password="x", role=Role.PATIENT, first_name="Anora")
        self.patient2 = Patient.objects.get(user=self.pu2)
        self.patient.blood_type = "A+"
        self.patient.save()
        self.record = MedicalRecord.objects.create(patient=self.patient, created_by=self.doctor2, title="T",
                                                   diagnosis="Maxfiy", visit_date=timezone.now())
        self.admin = User.objects.create_user(username="cadm", password="x", role=Role.ADMIN)

    def as_doc(self, d=None):
        self.client.force_authenticate(d or self.doctor)

    def closed_everywhere(self):
        """Shifokorga bemorning barcha klinik nuqtalari yopiqmi."""
        self.as_doc()
        p = self.patient.id
        r = self.client.get(f"/api/records/patients/{p}/")
        self.assertEqual((r.status_code, r.data.get("code")), (403, "consent_required"))
        self.assertEqual(self.client.get(f"/api/records/medical-records/?patient={p}").status_code, 403)
        self.assertEqual(self.client.get(f"/api/records/medical-records/{self.record.id}/").status_code, 403)
        self.assertEqual(self.client.get("/api/records/medical-records/").data, [])
        self.assertEqual(self.client.get(f"/api/records/prescriptions/?patient={p}").status_code, 403)
        self.assertEqual(self.client.get(f"/api/records/patients/{p}/history-pdf/").status_code, 403)
        self.assertEqual(self.client.get(f"/api/labs/results/?patient={p}").status_code, 403)
        self.assertEqual(self.client.post("/api/records/medical-records/", {
            "patient": p, "title": "X", "visit_date": timezone.now().isoformat()}, format="json").status_code, 403)

    def opened(self):
        self.as_doc()
        self.assertEqual(self.client.get(f"/api/records/patients/{self.patient.id}/").status_code, 200)
        self.assertEqual(len(self.client.get(f"/api/records/medical-records/?patient={self.patient.id}").data), 1)

    def test_no_consent_no_access(self):
        self.closed_everywhere()

    def test_admin_and_patient_unaffected(self):
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.get(f"/api/records/patients/{self.patient.id}/").status_code, 200)
        self.client.force_authenticate(self.pu)
        self.assertEqual(self.client.get(f"/api/records/patients/{self.patient.id}/").status_code, 200)

    def test_patient_grants_and_revokes(self):
        self.client.force_authenticate(self.pu)
        r = self.client.post("/api/records/consent/me/", {"doctor": self.doctor.id, "days": 7}, format="json")
        self.assertEqual(r.status_code, 201)
        self.opened()
        self.client.force_authenticate(self.pu2)                  # boshqa bemor bekor qila olmaydi
        self.assertEqual(self.client.post(f"/api/records/consent/me/{r.data['id']}/revoke/").status_code, 404)
        self.client.force_authenticate(self.pu)
        data = self.client.get("/api/records/consent/me/").data
        self.assertEqual(data["grants"][0]["doctor"]["name"], "Sardor")
        self.client.post(f"/api/records/consent/me/{r.data['id']}/revoke/")
        self.closed_everywhere()
        self.assertTrue(AuditLog.objects.filter(actor=self.pu, detail__startswith="Ruxsat bekor").exists())

    def test_grant_expires(self):
        from datetime import timedelta
        from unittest import mock
        self.client.force_authenticate(self.pu)
        self.client.post("/api/records/consent/me/", {"doctor": self.doctor.id, "days": 1}, format="json")
        self.opened()
        later = timezone.now() + timedelta(days=1, minutes=1)
        with mock.patch("django.utils.timezone.now", return_value=later):
            self.closed_everywhere()

    def test_bad_grant_input(self):
        self.client.force_authenticate(self.pu)
        self.assertEqual(self.client.post("/api/records/consent/me/", {"doctor": self.admin.id}, format="json").status_code, 400)
        self.assertEqual(self.client.post("/api/records/consent/me/", {"doctor": self.doctor.id, "days": 999}, format="json").status_code, 400)
        self.as_doc()
        self.assertEqual(self.client.post("/api/records/consent/me/", {"doctor": self.doctor.id}, format="json").status_code, 403)

    def test_todays_queue_ticket_gives_access(self):
        from appointments.models import DoctorProfile, QueueTicket
        from appointments.services import today
        DoctorProfile.objects.get_or_create(user=self.doctor)
        t = QueueTicket.objects.create(doctor=self.doctor, date=today(), number=1, source="online", patient=self.patient)
        self.opened()
        t.status = QueueTicket.CANCELLED
        t.save()
        self.closed_everywhere()

    def test_patient_list_for_doctor(self):
        CareGrant.objects.create(patient=self.patient2, doctor=self.doctor)
        self.as_doc()
        rows = self.client.get("/api/records/patients/").data
        self.assertEqual([r["id"] for r in rows], [self.patient2.id])            # faqat ruxsati borlar
        self.assertEqual(rows[0]["access_via"], "grant")
        rows = {r["id"]: r for r in self.client.get("/api/records/patients/", {"search": "cpat"}).data}
        self.assertFalse(rows[self.patient.id]["has_access"])
        self.assertNotIn("blood_type", rows[self.patient.id])                     # klinik maydon yo'q
        self.assertTrue(rows[self.patient2.id]["has_access"])

    def test_emergency_access(self):
        from datetime import timedelta
        from unittest import mock
        from monitoring.models import SecurityAlert
        from notifications.models import Notification
        self.as_doc()
        url = f"/api/records/patients/{self.patient.id}/emergency-access/"
        r = self.client.post(url, {"reason": "qisqa"}, format="json")
        self.assertEqual((r.status_code, r.data["code"]), (400, "reason_too_short"))
        r = self.client.post(url, {"reason": "Bemor hushsiz holatda keltirildi, allergiyalarni bilish zarur"}, format="json")
        self.assertEqual(r.status_code, 201)
        self.opened()
        self.assertEqual(self.client.get(f"/api/records/patients/{self.patient.id}/").data["access"]["via"], "emergency")
        self.assertTrue(AuditLog.objects.filter(actor=self.doctor, detail__startswith="FAVQULODDA KIRISH").exists())
        self.assertEqual(SecurityAlert.objects.filter(rule="emergency_access", actor=self.doctor, severity="high").count(), 1)
        n = Notification.objects.get(user=self.pu, kind="emergency_access")
        self.assertEqual(n.params["doctor"], "Sardor")
        self.client.force_authenticate(self.pu)
        self.assertTrue(self.client.get("/api/records/consent/me/").data["emergencies"][0]["active"])
        with mock.patch("django.utils.timezone.now", return_value=timezone.now() + timedelta(minutes=61)):
            self.closed_everywhere()
        self.client.force_authenticate(self.pu)                                   # bemor ishlata olmaydi
        self.assertEqual(self.client.post(url, {"reason": "x" * 30}, format="json").status_code, 403)

    @override_settings(PATIENT_CONSENT_REQUIRED=False)
    def test_setting_disables_consent(self):
        self.opened()

    def test_migration_backfills_existing_care(self):
        import importlib
        from django.apps import apps
        mig = importlib.import_module("records.migrations.0007_patient_consent")
        Prescription.objects.create(patient=self.patient2, prescribed_by=self.doctor, medication_name="D",
                                    dosage="1", duration_days=3, start_date=timezone.now().date())
        mig.backfill_existing_care(apps, None)
        pairs = set(CareGrant.objects.values_list("patient_id", "doctor_id"))
        self.assertEqual(pairs, {(self.patient.id, self.doctor2.id), (self.patient2.id, self.doctor.id)})


class PatientPhotoTests(APITestCase):
    """3x4 rasm: tekshirish, kesish, shifrlab saqlash va ruxsatlar."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.admin = User.objects.create_user(username="phadm", password="x", role=Role.ADMIN)
        self.doctor = User.objects.create_user(username="phdoc", password="x", role=Role.DOCTOR)
        self.lab = User.objects.create_user(username="phlab", password="x", role=Role.LAB)
        self.pu = User.objects.create_user(username="phpat", password="x", role=Role.PATIENT)
        self.patient = Patient.objects.get(user=self.pu)
        self.other = User.objects.create_user(username="phpat2", password="x", role=Role.PATIENT)
        self.url = f"/api/records/patients/{self.patient.id}/photo/"

    def image(self, size=(1200, 900), fmt="JPEG", name="p.jpg", exif=True):
        from io import BytesIO
        from django.core.files.uploadedfile import SimpleUploadedFile
        from PIL import Image
        img = Image.new("RGB", size, (200, 30, 30))
        img.paste((30, 30, 200), (size[0] // 2, 0, size[0], size[1]))      # o'ng yarmi ko'k
        buf = BytesIO()
        kw = {}
        if exif and fmt == "JPEG":
            ex = Image.Exif()
            ex[0x010F] = "SecretPhone"           # Make
            kw["exif"] = ex.tobytes()
        img.save(buf, fmt, **kw)
        return SimpleUploadedFile(name, buf.getvalue(), content_type=f"image/{fmt.lower()}")

    def upload(self, user, **extra):
        self.client.force_authenticate(user)
        return self.client.post(self.url, {"photo": self.image(), **extra}, format="multipart")

    def test_upload_crop_and_strip_metadata(self):
        from io import BytesIO
        from PIL import Image
        r = self.upload(self.admin, crop_x=0.5, crop_y=0, crop_w=0.5, crop_h=1)   # faqat ko'k yarmi
        self.assertEqual(r.status_code, 201, r.data)
        got = self.client.get(self.url)
        self.assertEqual((got.status_code, got["Content-Type"]), (200, "image/jpeg"))
        self.assertIn("no-store", got["Cache-Control"])
        img = Image.open(BytesIO(got.content))
        self.assertEqual(img.size, (600, 800))
        self.assertGreater(img.getpixel((300, 400))[2], 150)                     # ko'k qism kesib olingan
        self.assertNotIn(b"SecretPhone", got.content)
        self.assertFalse(img.getexif())
        self.assertTrue(AuditLog.objects.filter(actor=self.admin, patient=self.patient,
                                                detail="Bemor rasmi (3x4) yangilandi").exists())
        self.assertIsNotNone(self.client.get(f"/api/records/patients/{self.patient.id}/").data["photo_updated_at"])

    def test_encrypted_at_rest(self):
        from django.db import connection
        self.upload(self.pu)
        with connection.cursor() as c:
            c.execute("SELECT image FROM records_patientphoto WHERE patient_id=%s", [self.patient.id])
            raw = c.fetchone()[0]
        self.assertFalse(raw.startswith("/9j/"))                                  # JPEG base64 boshi ko'rinmaydi

    def test_crop_box_always_3x4_inside_image(self):
        from .photo import crop_box
        for w, h, crop in [(1200, 900, None), (900, 1600, None), (1000, 1000, {"x": 0.9, "y": 0.9, "w": 0.5, "h": 0.5}),
                           (800, 600, {"x": -1, "y": 2, "w": 9, "h": "x"})]:
            l, t, r, b = crop_box(w, h, crop)
            self.assertTrue(0 <= l < r <= w and 0 <= t < b <= h, (w, h, crop))
            self.assertAlmostEqual((r - l) / (b - t), 0.75, delta=0.02)

    def test_permissions(self):
        self.upload(self.pu)
        for user, code in [(self.admin, 200), (self.pu, 200), (self.lab, 403), (self.other, 403)]:
            self.client.force_authenticate(user)
            self.assertEqual(self.client.get(self.url).status_code, code, user.username)
        self.client.force_authenticate(self.doctor)                                 # roziligisiz yopiq
        r = self.client.get(self.url)
        self.assertEqual((r.status_code, r.data["code"]), (403, "consent_required"))
        self.assertEqual(self.upload(self.doctor).status_code, 403)                 # roziligisiz yuklay olmaydi
        CareGrant.objects.create(patient=self.patient, doctor=self.doctor)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.assertEqual(self.upload(self.doctor).status_code, 201)                 # davolovchi shifokor rasmga oladi
        self.client.force_authenticate(self.doctor)
        self.assertEqual(self.client.delete(self.url).status_code, 403)             # o'chira olmaydi
        for user in (self.other, self.lab):
            self.assertEqual(self.upload(user).status_code, 403, user.username)

    def test_bad_files(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        self.client.force_authenticate(self.pu)
        r = self.client.post(self.url, {"photo": SimpleUploadedFile("x.jpg", b"not an image")}, format="multipart")
        self.assertEqual(r.status_code, 400)
        r = self.client.post(self.url, {"photo": self.image(name="p.gif")}, format="multipart")
        self.assertEqual(r.status_code, 400)
        r = self.client.post(self.url, {"photo": self.image(size=(60, 80))}, format="multipart")
        self.assertIn("kichik", r.data["detail"])
        self.assertEqual(self.client.post(self.url, {}, format="multipart").status_code, 400)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_delete(self):
        self.upload(self.pu)
        self.client.force_authenticate(self.other)
        self.assertEqual(self.client.delete(self.url).status_code, 403)
        self.client.force_authenticate(self.pu)
        self.assertEqual(self.client.delete(self.url).status_code, 204)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertTrue(AuditLog.objects.filter(action="delete", detail="Bemor rasmi (3x4) o'chirildi").exists())

    def test_registry_search_shows_photo_time(self):
        self.upload(self.pu)
        self.pu.first_name = "Rasmli"
        self.pu.save()
        self.client.force_authenticate(self.admin)
        rows = self.client.get("/api/queue/registry/patients/", {"q": "rasmli"}).data
        self.assertIsNotNone(rows[0]["photo_updated_at"])



class PatientProfileEditRightsTests(APITestCase):
    """Minimal huquq: shifokor - tibbiy maydonlar, admin - shaxsiy ma'lumotlar ham."""

    def setUp(self):
        self.doctor = User.objects.create_user(username="eddoc", password="x", role=Role.DOCTOR)
        self.admin = User.objects.create_user(username="edadm", password="x", role=Role.ADMIN)
        self.pu = User.objects.create_user(username="edpat", password="x", role=Role.PATIENT)
        self.patient = Patient.objects.get(user=self.pu)
        self.url = f"/api/records/patients/{self.patient.id}/"
        CareGrant.objects.create(patient=self.patient, doctor=self.doctor)

    def patch(self, user, data):
        self.client.force_authenticate(user)
        return self.client.patch(self.url, data, format="json")

    def test_doctor_edits_medical_fields_only(self):
        r = self.patch(self.doctor, {"allergies": "Penitsillin", "blood_type": "A+"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(AuditLog.objects.filter(actor=self.doctor,
                                                detail="Bemor profili tahrirlandi: qon guruhi, allergiyalar").exists())
        for field, value in [("national_id", "AA1234567"), ("address", "Toshkent"), ("date_of_birth", "1990-01-01"),
                             ("gender", "F"), ("emergency_contact", "+998901112233"), ("primary_doctor", self.doctor.id)]:
            r = self.patch(self.doctor, {"allergies": "x", field: value})
            self.assertEqual((r.status_code, r.data["code"], r.data["fields"]), (403, "field_not_allowed", [field]), field)
        self.patient.refresh_from_db()
        self.assertEqual((self.patient.national_id, self.patient.allergies), ("", "Penitsillin"))

    def test_admin_edits_everything_and_values_not_logged(self):
        r = self.patch(self.admin, {"national_id": "AA1234567", "address": "Toshkent", "date_of_birth": "1990-01-01"})
        self.assertEqual(r.status_code, 200)
        log = AuditLog.objects.filter(actor=self.admin).latest("id")
        self.assertIn("hujjat raqami", log.detail)
        self.assertNotIn("AA1234567", log.detail)

    def test_doctor_without_consent_and_patient_cannot_edit(self):
        CareGrant.objects.all().delete()
        self.assertEqual(self.patch(self.doctor, {"allergies": "x"}).data["code"], "consent_required")
        self.assertEqual(self.patch(self.pu, {"allergies": "x"}).status_code, 403)


class ImportAvatarPhotosTests(APITestCase):
    def test_copies_avatar_once_and_keeps_existing_photos(self):
        from io import BytesIO, StringIO
        from django.core.files.base import ContentFile
        from django.core.management import call_command
        from PIL import Image
        from .models import PatientPhoto

        def jpeg(size, color):
            buf = BytesIO()
            Image.new("RGB", size, color).save(buf, "JPEG")
            return ContentFile(buf.getvalue())

        a = User.objects.create_user(username="ava1", password="x", role=Role.PATIENT)
        a.avatar.save("avatars/0123456789abcdef0123456789abcdef.jpg", jpeg((384, 512), (10, 200, 10)))
        b = User.objects.create_user(username="ava2", password="x", role=Role.PATIENT)
        b.avatar.save("avatars/fedcba9876543210fedcba9876543210.jpg", jpeg((512, 512), (0, 0, 0)))
        PatientPhoto.objects.create(patient=Patient.objects.get(user=b), image="eA==", updated_at=timezone.now())
        User.objects.create_user(username="ava3", password="x", role=Role.PATIENT)          # avatarsiz

        out = StringIO()
        call_command("import_avatar_photos", "--dry-run", stdout=out)
        self.assertEqual(PatientPhoto.objects.count(), 1)
        self.assertIn("1 ta bemor", out.getvalue())

        call_command("import_avatar_photos", stdout=StringIO())
        photo = PatientPhoto.objects.get(patient__user=a)
        self.client.force_authenticate(a)
        img = Image.open(BytesIO(self.client.get(f"/api/records/patients/{photo.patient_id}/photo/").content))
        self.assertEqual(img.size, (600, 800))
        self.assertGreater(img.getpixel((300, 400))[1], 150)
        self.assertEqual(PatientPhoto.objects.get(patient__user=b).image, "eA==")             # ustidan yozilmadi
        self.assertTrue(a.avatar.storage.exists(a.avatar.name))                              # avatar joyida
        call_command("import_avatar_photos", stdout=StringIO())                               # qayta - hech narsa
        self.assertEqual(PatientPhoto.objects.count(), 2)
