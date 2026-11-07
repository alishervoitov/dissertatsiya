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


@override_settings(TELEGRAM_BOT_TOKEN="123:SECRET", TELEGRAM_AUDIT_CHAT_ID="42", TELEGRAM_ALERT_CHAT_ID="",
                   FRONTEND_URL="https://medsecure.uz")
@mock.patch("rest_framework.throttling.ScopedRateThrottle.allow_request", return_value=True)
class TelegramAlertTests(APITestCase):
    """Xavfsizlik hodisalari administratorning Telegram'iga real vaqtda yuboriladi."""

    PW = "Qx7#vTm2!pLz9w"

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.sent = []
        patcher = mock.patch("monitoring.telegram.send", side_effect=self._send)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.doctor = User.objects.create_user(username="tg_doc", password=self.PW, role=Role.DOCTOR)

    def _send(self, text, chat_id):
        self.sent.append((chat_id, text))
        return len(self.sent)

    def make_alert(self, **kw):
        from django.utils import timezone
        now = timezone.now()
        data = dict(rule=SecurityAlert.EMERGENCY_ACCESS, severity="high", actor=self.doctor, actor_role="doctor",
                    detail="Bemor #7: <b>hushsiz</b> keltirildi", first_seen=now, last_seen=now)
        data.update(kw)
        with self.captureOnCommitCallbacks(execute=True):
            return SecurityAlert.objects.create(**data)

    def test_new_alert_is_sent_escaped(self, _):
        self.make_alert()
        self.assertEqual(len(self.sent), 1)
        chat, text = self.sent[0]
        self.assertEqual(chat, "42")                              # alohida chat berilmasa - audit chatiga
        self.assertIn("🔴", text)
        self.assertIn("Favqulodda kirish", text)
        self.assertIn("tg_doc", text)
        self.assertIn("&lt;b&gt;hushsiz&lt;/b&gt;", text)        # foydalanuvchi matni HTML sifatida ishlamaydi
        self.assertIn("https://medsecure.uz/admin/alerts", text)

    @override_settings(TELEGRAM_ALERT_CHAT_ID="99")
    def test_separate_alert_chat(self, _):
        self.make_alert()
        self.assertEqual(self.sent[0][0], "99")

    def test_counter_update_and_audit_tamper_not_sent(self, _):
        alert = self.make_alert()
        with self.captureOnCommitCallbacks(execute=True):
            alert.count += 1
            alert.save(update_fields=["count"])
        self.make_alert(rule=SecurityAlert.AUDIT_TAMPER, actor=None)
        self.assertEqual(len(self.sent), 1)

    def test_detector_alert_is_sent(self, _):
        patient = Patient.objects.get(user=User.objects.create_user(username="tg_p", password="x", role=Role.PATIENT))
        with mock.patch("django.utils.timezone.now", return_value=at(23, 15)), \
                self.captureOnCommitCallbacks(execute=True):
            log_audit(self.doctor, "view", patient=patient)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("🟠", self.sent[0][1])                     # tungi murojaat - o'rta daraja

    @override_settings(TELEGRAM_BOT_TOKEN="")
    def test_not_configured_sends_nothing(self, _):
        self.make_alert()
        self.assertEqual(self.sent, [])

    def login(self, username, password):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post("/api/auth/login/", {"username": username, "password": password},
                                    format="json", HTTP_X_FORWARDED_FOR="203.0.113.9")

    def test_login_lockout_sent_once_with_account_hint(self, _):
        from users.security import MAX_FAILED_ATTEMPTS
        for _i in range(MAX_FAILED_ATTEMPTS + 2):
            self.login("tg_doc", "wrong")
        for _i in range(MAX_FAILED_ATTEMPTS):
            self.login("<i>ghost</i>", "wrong")
        self.assertEqual(len(self.sent), 2)
        real, ghost = self.sent[0][1], self.sent[1][1]
        self.assertIn("login bloklandi", real)
        self.assertIn("hisob mavjud, rol: doctor", real)
        self.assertIn("203.0.113.9", real)
        self.assertIn("hisob mavjud emas", ghost)
        self.assertIn("&lt;i&gt;ghost&lt;/i&gt;", ghost)

    def mfa_login(self):
        from django.utils import timezone
        from users import mfa
        from users.models import TwoFactorDevice
        secret = mfa.new_secret()
        TwoFactorDevice.objects.create(user=self.doctor, secret=secret, confirmed_at=timezone.now())
        self.doctor.is_active_2fa = True
        self.doctor.save()
        r = self.login("tg_doc", self.PW)
        self.assertTrue(r.data["mfa_required"])
        return r.data["mfa_token"]

    def test_mfa_exhausted_sent(self, _):
        from users import mfa
        token = self.mfa_login()
        for _i in range(mfa.MFA_MAX_ATTEMPTS):
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post("/api/auth/login/2fa/", {"mfa_token": token, "code": "000000"}, format="json")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("2FA kodi 5 marta noto'g'ri", self.sent[0][1])
        self.assertIn("Parol to'g'ri kiritilgan", self.sent[0][1])

    def test_backup_code_login_sent(self, _):
        from users import mfa
        token = self.mfa_login()
        code = mfa.new_backup_codes(self.doctor)[0]
        with self.captureOnCommitCallbacks(execute=True):
            r = self.client.post("/api/auth/login/2fa/", {"mfa_token": token, "code": code}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn("zaxira kod bilan kirildi", self.sent[-1][1])

    def test_flood_is_capped(self, _):
        from monitoring.notify import ALERT_RATE_LIMIT, notify
        with self.captureOnCommitCallbacks(execute=True):
            for i in range(ALERT_RATE_LIMIT + 10):
                notify(f"hodisa {i}")
        self.assertEqual(len(self.sent), ALERT_RATE_LIMIT + 1)
        self.assertIn("juda ko'p xavfsizlik hodisasi", self.sent[-1][1])

    def test_telegram_down_does_not_break_request(self, _):
        from monitoring import telegram
        with mock.patch("monitoring.telegram.send", side_effect=telegram.TelegramError("Tarmoq xatosi")):
            from users.security import MAX_FAILED_ATTEMPTS
            for _i in range(MAX_FAILED_ATTEMPTS):
                r = self.login("tg_doc", "wrong")
        self.assertEqual(r.status_code, 401)
