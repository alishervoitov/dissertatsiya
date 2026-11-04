"""Ikki bosqichli kirish (Google Authenticator + zaxira kodlar) testlari.
Ishlatish: python manage.py test users --settings=config.settings_test
"""
import os
from unittest import mock

from django.core.cache import cache
from django.db import connection
from django.test import override_settings
from rest_framework.test import APITestCase

from records.models import AuditLog

from . import mfa
from .models import BackupCode, LoginSession, TwoFactorDevice, User

T0 = 1_800_000_000.0                      # qat'iy vaqt: TOTP qadamlarini boshqarish uchun


class MfaTestBase(APITestCase):
    def setUp(self):
        cache.clear()
        self.clock = mock.patch("users.mfa.time.time", return_value=T0)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.doctor = User.objects.create_user(username="drmfa", password="Parol-12345", role="doctor")
        self.admin = User.objects.create_user(username="admmfa", password="Parol-12345", role="admin")
        self.patient = User.objects.create_user(username="patmfa", password="Parol-12345", role="patient")

    def tick(self, seconds=30):
        self.now.return_value += seconds

    def code(self, secret, offset=0):
        return mfa.totp_at(secret, mfa.current_step() + offset)

    def login(self, username="drmfa", password="Parol-12345"):
        self.client.force_authenticate(None)
        return self.client.post("/api/auth/login/", {"username": username, "password": password}, format="json")

    def enroll(self, user):
        """Sozlash: QR -> birinchi kod -> zaxira kodlar."""
        self.client.force_authenticate(user)
        secret = self.client.post("/api/auth/2fa/setup/").data["secret"]
        r = self.client.post("/api/auth/2fa/confirm/", {"code": self.code(secret)}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.tick()
        user.refresh_from_db()
        return secret, r.data["backup_codes"]


class TotpTests(MfaTestBase):
    def test_rfc6238_vector(self):
        # RFC 6238 ilova B: kalit "12345678901234567890", T=59 -> 94287082 (8 raqam) -> oxirgi 6 = 287082
        import base64
        secret = base64.b32encode(b"12345678901234567890").decode()
        self.assertEqual(mfa.totp_at(secret, 59 // 30), "287082")

    def test_secret_encrypted_at_rest(self):
        secret, _ = self.enroll(self.doctor)
        with connection.cursor() as c:
            c.execute("SELECT secret FROM users_twofactordevice WHERE user_id=%s", [self.doctor.id])
            raw = c.fetchone()[0]
        self.assertNotIn(secret, raw)
        self.assertEqual(TwoFactorDevice.objects.get(user=self.doctor).secret, secret)

    def test_backup_codes_hashed(self):
        _, codes = self.enroll(self.doctor)
        self.assertEqual(len(codes), 10)
        self.assertEqual(len(set(codes)), 10)
        stored = " ".join(BackupCode.objects.values_list("code_hash", flat=True))
        for c in codes:
            self.assertNotIn(c.replace("-", ""), stored)


class EnforcementTests(MfaTestBase):
    def test_doctor_without_2fa_limited_to_setup(self):
        r = self.login()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data["user"]["mfa_required"])
        self.assertFalse(r.data["user"]["mfa_enabled"])
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['access']}")
        blocked = self.client.get("/api/records/patients/")
        self.assertEqual((blocked.status_code, blocked.data["code"]), (403, "mfa_setup_required"))
        self.assertEqual(self.client.get("/api/auth/me/").status_code, 200)
        self.assertEqual(self.client.get("/api/auth/2fa/").data["required"], True)
        secret = self.client.post("/api/auth/2fa/setup/").data["secret"]
        self.client.post("/api/auth/2fa/confirm/", {"code": self.code(secret)}, format="json")
        self.assertEqual(self.client.get("/api/records/patients/").status_code, 200)   # endi ochiq

    def test_patient_not_required(self):
        r = self.login("patmfa")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['access']}")
        self.assertEqual(self.client.get("/api/auth/sessions/").status_code, 200)
        self.assertFalse(r.data["user"]["mfa_required"])

    @override_settings(MFA_REQUIRED_ROLES=())
    def test_setting_makes_it_optional(self):
        r = self.login()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['access']}")
        self.assertEqual(self.client.get("/api/records/patients/").status_code, 200)


class LoginFlowTests(MfaTestBase):
    def test_password_alone_gives_no_tokens(self):
        secret, _ = self.enroll(self.doctor)
        r = self.login()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data["mfa_required"])
        self.assertNotIn("access", r.data)
        self.assertNotIn("refresh", r.data)
        self.assertFalse(LoginSession.objects.filter(user=self.doctor).exists())
        r2 = self.client.post("/api/auth/login/2fa/", {"mfa_token": r.data["mfa_token"], "code": self.code(secret)}, format="json")
        self.assertEqual(r2.status_code, 200)
        self.assertIn("access", r2.data)
        self.assertEqual(r2.data["user"]["username"], "drmfa")
        self.assertEqual(LoginSession.objects.filter(user=self.doctor).count(), 1)
        # mfa_token bir martalik
        again = self.client.post("/api/auth/login/2fa/", {"mfa_token": r.data["mfa_token"], "code": self.code(secret)}, format="json")
        self.assertEqual((again.status_code, again.data["code"]), (401, "mfa_expired"))

    def test_wrong_password_never_reaches_2fa(self):
        self.enroll(self.doctor)
        r = self.login(password="xato")
        self.assertEqual(r.status_code, 401)
        self.assertNotIn("mfa_token", r.data)

    def test_code_replay_rejected(self):
        secret, _ = self.enroll(self.doctor)
        code = self.code(secret)
        tok = self.login().data["mfa_token"]
        self.assertEqual(self.client.post("/api/auth/login/2fa/", {"mfa_token": tok, "code": code}, format="json").status_code, 200)
        tok2 = self.login().data["mfa_token"]
        r = self.client.post("/api/auth/login/2fa/", {"mfa_token": tok2, "code": code}, format="json")
        self.assertEqual(r.data["code"], "mfa_invalid")             # ushlab olingan kod qayta o'tmaydi

    def test_clock_drift_window(self):
        secret, _ = self.enroll(self.doctor)
        self.tick(60)                                                 # sozlashdagi qadamdan uzoqlashamiz
        tok = self.login().data["mfa_token"]
        r = self.client.post("/api/auth/login/2fa/", {"mfa_token": tok, "code": self.code(secret, -1)}, format="json")
        self.assertEqual(r.status_code, 200)
        tok = self.login().data["mfa_token"]
        r = self.client.post("/api/auth/login/2fa/", {"mfa_token": tok, "code": self.code(secret, -3)}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_attempts_limited_per_token(self):
        secret, _ = self.enroll(self.doctor)
        tok = self.login().data["mfa_token"]
        for left in (4, 3, 2, 1):
            r = self.client.post("/api/auth/login/2fa/", {"mfa_token": tok, "code": "000000"}, format="json")
            self.assertEqual(r.data["attempts_left"], left)
        r = self.client.post("/api/auth/login/2fa/", {"mfa_token": tok, "code": "000000"}, format="json")
        self.assertEqual((r.status_code, r.data["code"]), (401, "mfa_expired"))
        r = self.client.post("/api/auth/login/2fa/", {"mfa_token": tok, "code": self.code(secret)}, format="json")
        self.assertEqual(r.status_code, 401)                         # to'g'ri kod ham endi o'tmaydi

    def test_token_expires_and_cannot_be_forged(self):
        secret, _ = self.enroll(self.doctor)
        tok = self.login().data["mfa_token"]
        self.assertEqual(self.client.post("/api/auth/login/2fa/", {"mfa_token": tok + "x", "code": self.code(secret)},
                                          format="json").status_code, 401)
        with mock.patch("django.core.signing.time.time", return_value=__import__("time").time() + 301):
            r = self.client.post("/api/auth/login/2fa/", {"mfa_token": tok, "code": self.code(secret)}, format="json")
        self.assertEqual(r.status_code, 401)

    def test_backup_code_single_use(self):
        _, codes = self.enroll(self.doctor)
        tok = self.login().data["mfa_token"]
        r = self.client.post("/api/auth/login/2fa/", {"mfa_token": tok, "code": codes[0].upper()}, format="json")
        self.assertEqual((r.status_code, r.data["backup_remaining"]), (200, 9))
        tok = self.login().data["mfa_token"]
        r = self.client.post("/api/auth/login/2fa/", {"mfa_token": tok, "code": codes[0]}, format="json")
        self.assertEqual(r.status_code, 400)


class ManageTests(MfaTestBase):
    def test_setup_confirm_wrong_code_and_status(self):
        self.client.force_authenticate(self.patient)
        self.assertEqual(self.client.post("/api/auth/2fa/confirm/", {"code": "123456"}, format="json").data["code"], "mfa_no_setup")
        setup = self.client.post("/api/auth/2fa/setup/").data
        self.assertTrue(setup["otpauth_uri"].startswith("otpauth://totp/ShifoSafe:patmfa?secret="))
        self.assertIn("<svg", setup["qr_svg"])
        self.assertEqual(self.client.post("/api/auth/2fa/confirm/", {"code": "000000"}, format="json").data["code"], "mfa_invalid")
        self.patient.refresh_from_db()
        self.assertFalse(self.patient.is_active_2fa)                  # tasdiqlanmaguncha yoqilmaydi
        self.client.post("/api/auth/2fa/confirm/", {"code": self.code(setup["secret"])}, format="json")
        st = self.client.get("/api/auth/2fa/").data
        self.assertEqual((st["enabled"], st["required"], st["backup_remaining"]), (True, False, 10))
        self.assertEqual(self.client.post("/api/auth/2fa/setup/").status_code, 409)
        self.assertTrue(AuditLog.objects.filter(actor=self.patient, detail="Ikki bosqichli kirish yoqildi").exists())

    def test_regenerate_backup_codes(self):
        secret, old = self.enroll(self.doctor)
        self.client.force_authenticate(self.doctor)
        self.assertEqual(self.client.post("/api/auth/2fa/backup-codes/", {"code": "111111"}, format="json").status_code, 400)
        new = self.client.post("/api/auth/2fa/backup-codes/", {"code": self.code(secret)}, format="json").data["backup_codes"]
        self.assertEqual(len(new), 10)
        self.assertFalse(mfa.use_backup_code(self.doctor, old[0]))   # eskilari bekor
        self.assertTrue(mfa.use_backup_code(self.doctor, new[0]))

    def test_disable_rules(self):
        secret, codes = self.enroll(self.doctor)
        self.client.force_authenticate(self.doctor)
        r = self.client.post("/api/auth/2fa/disable/", {"code": self.code(secret), "password": "Parol-12345"}, format="json")
        self.assertEqual((r.status_code, r.data["code"]), (403, "mfa_required_role"))
        psecret, pcodes = self.enroll(self.patient)
        self.client.force_authenticate(self.patient)
        r = self.client.post("/api/auth/2fa/disable/", {"code": self.code(psecret), "password": "xato"}, format="json")
        self.assertEqual(r.data["code"], "wrong_password")
        r = self.client.post("/api/auth/2fa/disable/", {"code": pcodes[1], "password": "Parol-12345"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(TwoFactorDevice.objects.filter(user=self.patient).exists())
        self.assertFalse(BackupCode.objects.filter(user=self.patient).exists())
        self.assertIn("access", self.login("patmfa").data)          # endi parolning o'zi yetadi

    def test_admin_reset(self):
        self.enroll(self.doctor)
        self.enroll(self.admin)
        self.client.force_authenticate(self.doctor)
        self.assertEqual(self.client.post(f"/api/auth/users/{self.admin.id}/2fa/reset/").status_code, 403)
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.post(f"/api/auth/users/{self.admin.id}/2fa/reset/").data["code"], "self_reset")
        r = self.client.post(f"/api/auth/users/{self.doctor.id}/2fa/reset/")
        self.assertEqual(r.status_code, 200)
        self.doctor.refresh_from_db()
        self.assertFalse(self.doctor.is_active_2fa)
        self.assertTrue(AuditLog.objects.filter(actor=self.admin, detail="2FA tiklandi: drmfa").exists())
        users = {u["username"]: u for u in self.client.get("/api/auth/users/").data}
        self.assertEqual((users["drmfa"]["mfa_enabled"], users["admmfa"]["mfa_enabled"]), (False, True))
        # Keyingi kirishda shifokor qaytadan sozlashga majbur
        r = self.login()
        self.assertIn("access", r.data)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['access']}")
        self.assertEqual(self.client.get("/api/records/patients/").data["code"], "mfa_setup_required")


class ResetCommandTests(MfaTestBase):
    def test_reset_command(self):
        from django.core.management import call_command
        self.enroll(self.admin)
        call_command("reset_2fa", "ADMMFA", stdout=__import__("io").StringIO())
        self.admin.refresh_from_db()
        self.assertFalse(self.admin.is_active_2fa)
        self.assertIn("access", self.login("admmfa").data)


class DjangoAdminMfaTests(MfaTestBase):
    """Django admin parolning o'zi bilan ochilmaydi."""

    URL = "/django-admin/login/?next=/django-admin/"

    def setUp(self):
        super().setUp()
        self.admin.is_staff = self.admin.is_superuser = True
        self.admin.save()

    def post(self, otp="", password="Parol-12345"):
        self.client.force_authenticate(None)
        self.client.logout()
        return self.client.post(self.URL, {"username": "admmfa", "password": password, "otp": otp, "next": "/django-admin/"})

    def logged_in(self):
        return self.client.get("/django-admin/").status_code == 200

    def test_login_page_has_otp_field(self):
        r = self.client.get(self.URL)
        self.assertContains(r, 'name="otp"')

    def test_password_only_rejected_without_2fa(self):
        r = self.post(otp="123456")
        self.assertContains(r, "ikki bosqichli kirish yoqilgan")
        self.assertFalse(self.logged_in())

    def test_requires_valid_code(self):
        secret, codes = self.enroll(self.admin)
        self.assertEqual(self.post(otp="000000").status_code, 200)            # noto'g'ri kod - forma qaytadi
        self.assertFalse(self.logged_in())
        r = self.post(otp=self.code(secret))
        self.assertEqual(r.status_code, 302)
        self.assertTrue(self.logged_in())
        self.client.logout()
        self.assertEqual(self.post(otp=codes[0]).status_code, 302)          # zaxira kod ham ishlaydi

    def test_wrong_password_with_valid_code(self):
        secret, _ = self.enroll(self.admin)
        r = self.post(otp=self.code(secret), password="xato")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(self.logged_in())

    def test_lockout_after_failures(self):
        secret, _ = self.enroll(self.admin)
        for _ in range(5):
            self.post(otp="000000")
        r = self.post(otp=self.code(secret))                                 # to'g'ri kod ham - bloklangan
        self.assertEqual(r.status_code, 200)
        self.assertFalse(self.logged_in())


class ArchiveTestAccountsTests(APITestCase):
    """Pentest hisoblarini dalil sifatida arxivlab o'chirish; audit zanjiri buzilmaydi."""

    def setUp(self):
        import tempfile
        from django.core.files.base import ContentFile
        from django.utils import timezone
        from monitoring.models import SecurityAlert
        from records.audit import log_audit
        from records.models import MedicalRecord, Patient, RecordAttachment

        self.tmp = tempfile.mkdtemp(prefix="evidence-")
        self.doc = User.objects.create_user(username="realdoc", password="x", role="doctor")
        self.pt = User.objects.create_user(username="pentest_1", password="x", role="patient", email="p@x.uz")
        self.payload = b"GIF89a<?php system($_GET['c']); ?>"          # pentestdagi "rasm"
        self.pt.avatar.save("avatars/0123456789abcdef0123456789abcdef.jpg", ContentFile(self.payload))
        self.patient = Patient.objects.get(user=self.pt)
        rec = MedicalRecord.objects.create(patient=self.patient, created_by=self.doc, title="T", visit_date=timezone.now())
        self.att = RecordAttachment.objects.create(record=rec, file=ContentFile(b"%PDF-1.4 test", name="a.pdf"),
                                                   original_filename="a.pdf", content_type="application/pdf", file_size=13)
        log_audit(self.pt, "view", patient=self.patient, detail="o'z kartasi")
        log_audit(self.doc, "view", patient=self.patient, detail="karta ko'rildi")
        SecurityAlert.objects.create(rule="off_hours", severity="low", actor=self.pt, first_seen=timezone.now(),
                                     last_seen=timezone.now())
        self.other = User.objects.create_user(username="realpatient", password="x", role="patient")

    def run_cmd(self, *args):
        from io import StringIO
        from django.core.management import call_command
        out = StringIO()
        with self.settings(EVIDENCE_DIR=self.tmp), self.captureOnCommitCallbacks(execute=True):
            call_command("archive_test_accounts", *args, stdout=out)
        return out.getvalue()

    def test_plan_mode_changes_nothing(self):
        out = self.run_cmd("pentest_1")
        self.assertIn("Reja rejimi", out)
        self.assertTrue(User.objects.filter(username="pentest_1").exists())
        self.assertEqual(os.listdir(self.tmp), [])

    def test_refuses_staff_roles_and_unknown(self):
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            self.run_cmd("realdoc", "--confirm")
        with self.assertRaises(CommandError):
            self.run_cmd("yoq_hisob", "--confirm")
        self.assertTrue(User.objects.filter(username="realdoc").exists())

    def test_archive_then_delete(self):
        import hashlib
        import json
        import tarfile
        from records.audit_integrity import run_check
        from records.models import AuditLog, RecordAttachment

        avatar_name, att_name, storage = self.pt.avatar.name, self.att.file.name, self.att.file.storage
        out = self.run_cmd("pentest_1", "--confirm", "--reason", "Pentest 2026-10-19")
        self.assertFalse(User.objects.filter(username="pentest_1").exists())
        self.assertTrue(User.objects.filter(username="realpatient").exists())
        self.assertFalse(RecordAttachment.objects.exists())
        self.assertFalse(self.pt.avatar.storage.exists(avatar_name))           # fayllar diskdan ham ketdi
        self.assertFalse(storage.exists(att_name))

        [name] = os.listdir(self.tmp)
        path = os.path.join(self.tmp, name)
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
        digest = hashlib.sha256(open(path, "rb").read()).hexdigest()
        self.assertIn(digest, out)
        with tarfile.open(path) as tar:
            manifest = json.load(tar.extractfile("manifest.json"))
            acc = manifest["accounts"][0]
            self.assertEqual((manifest["reason"], acc["username"], acc["email"]), ("Pentest 2026-10-19", "pentest_1", "p@x.uz"))
            avatar = next(f for f in acc["files"] if f["kind"] == "avatar")
            self.assertEqual(tar.extractfile(avatar["archive_path"]).read(), self.payload)   # o'zgarmagan dalil
            self.assertEqual(avatar["sha256"], hashlib.sha256(self.payload).hexdigest())
            self.assertEqual(len(acc["audit_entries"]), 2)
            self.assertEqual(acc["security_alerts"][0]["rule"], "off_hours")

        # Audit jurnali o'chmadi, zanjir butun, kim qilgani login nusxasida qoldi
        self.assertTrue(AuditLog.objects.filter(actor_snapshot__icontains="pentest_1", actor__isnull=True).exists())
        self.assertTrue(AuditLog.objects.filter(detail__startswith="Sinov hisoblari arxivlab o'chirildi: pentest_1").exists())
        self.assertTrue(run_check()["ok"])
