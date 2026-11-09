"""
Shubhali faollik qoidalari testlari.
Ishlatish: python manage.py test monitoring --settings=config.settings_test
"""
from datetime import datetime, timedelta
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


@override_settings(SECURITY_ALERTS={"MASS_ACCESS_PATIENTS": 3, "MASS_ACCESS_MINUTES": 10, "EXPORT_COUNT": 2, "EXPORT_MINUTES": 30})
@mock.patch("rest_framework.throttling.ScopedRateThrottle.allow_request", return_value=True)
class AutoResponseTests(APITestCase):
    """I3: yuqori darajali ogohlantirishda xodimning sessiyalari darhol yopiladi va hisob to'xtatiladi."""

    PW = "Qx7#vTm2!pLz9w"

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.clock = mock.patch("django.utils.timezone.now", return_value=at(11))
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.doctor = User.objects.create_user(username="ar_doc", password=self.PW, role=Role.DOCTOR, is_active_2fa=True)
        self.admin = User.objects.create_user(username="ar_adm", password=self.PW, role=Role.ADMIN, is_active_2fa=True)
        self.patients = [Patient.objects.get(user=User.objects.create_user(username=f"arp{i}", password="x",
                                                                              role=Role.PATIENT)) for i in range(4)]

    def session_for(self, user):
        """Haqiqiy login kabi: refresh token + LoginSession; access tokenni qaytaradi."""
        from rest_framework.test import APIRequestFactory
        from users import sessions
        from users.serializers import CustomTokenObtainPairSerializer
        refresh = CustomTokenObtainPairSerializer.get_token(user)
        sessions.start_session(APIRequestFactory().get("/"), user, refresh)
        return str(refresh.access_token)

    def me(self, access):
        return self.client.get("/api/auth/me/", HTTP_AUTHORIZATION=f"Bearer {access}")

    def mass_view(self, user):
        for p in self.patients[:3]:
            log_audit(user, "view", patient=p)

    def login(self, user):
        return self.client.post("/api/auth/login/", {"username": user.username, "password": self.PW}, format="json",
                                HTTP_ACCEPT_LANGUAGE="uz")

    def test_doctor_sessions_closed_and_account_locked_until_ack(self, _):
        from users.models import LoginSession
        access = self.session_for(self.doctor)
        self.assertEqual(self.me(access).status_code, 200)
        self.mass_view(self.doctor)
        alert = SecurityAlert.objects.get(rule="mass_access")
        self.assertEqual(alert.auto_response, "locked")
        self.assertEqual(self.me(access).status_code, 401)                       # qo'ldagi token darhol o'ldi
        self.assertFalse(LoginSession.objects.filter(user=self.doctor, revoked_at__isnull=True).exists())
        r = self.login(self.doctor)                                               # to'g'ri parol bilan ham
        self.assertEqual(r.status_code, 401)
        self.assertIn("vaqtincha to'xtatildi", str(r.data["detail"]))
        self.assertTrue(AuditLog.objects.filter(detail__startswith="Avtomatik javob: ar_doc - hisob to'xtatildi").exists())

        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.post(f"/api/monitoring/alerts/{alert.id}/ack/").data["auto_response"], "locked")
        self.client.force_authenticate(None)
        self.assertIn("access", self.login(self.doctor).data)                    # ko'rib chiqilgach tiklandi
        self.assertTrue(AuditLog.objects.filter(actor=self.admin, detail="Hisob qayta faollashtirildi: ar_doc").exists())

    def test_wrong_password_does_not_reveal_suspension(self, _):
        self.mass_view(self.doctor)
        r = self.client.post("/api/auth/login/", {"username": "ar_doc", "password": "wrong"}, format="json",
                             HTTP_ACCEPT_LANGUAGE="uz")
        self.assertNotIn("to'xtatildi", str(r.data))
        ru = self.client.post("/api/auth/login/", {"username": "ar_doc", "password": self.PW}, format="json",
                              HTTP_ACCEPT_LANGUAGE="ru")
        self.assertIn("временно приостановлена", str(ru.data["detail"]))

    def test_admin_only_logged_out_not_locked(self, _):
        access = self.session_for(self.admin)
        self.mass_view(self.admin)
        self.assertEqual(SecurityAlert.objects.get().auto_response, "logout")
        self.assertEqual(self.me(access).status_code, 401)
        self.assertIn("access", self.login(self.admin).data)                     # o'zini bloklab qo'ymaydi

    def test_medium_alert_has_no_response(self, _):
        access = self.session_for(self.doctor)
        self.now.return_value = at(23)
        log_audit(self.doctor, "view", patient=self.patients[0])
        self.assertEqual(SecurityAlert.objects.get(rule="off_hours").auto_response, "")
        self.assertEqual(self.me(access).status_code, 200)

    @override_settings(SECURITY_ALERTS={"MASS_ACCESS_PATIENTS": 3, "AUTO_SUSPEND_RULES": ()})
    def test_can_be_disabled(self, _):
        access = self.session_for(self.doctor)
        self.mass_view(self.doctor)
        self.assertEqual(SecurityAlert.objects.get(rule="mass_access").auto_response, "")
        self.assertEqual(self.me(access).status_code, 200)

    def test_pending_2fa_step_is_blocked_too(self, _):
        from users import mfa
        from users.models import TwoFactorDevice
        TwoFactorDevice.objects.create(user=self.doctor, secret=mfa.new_secret(), confirmed_at=at(9))
        token = self.login(self.doctor).data["mfa_token"]                          # parol bosqichi o'tdi
        self.mass_view(self.doctor)                                              # shu orada hisob to'xtatildi
        code = mfa.totp_at(TwoFactorDevice.objects.get(user=self.doctor).secret, mfa.current_step())
        r = self.client.post("/api/auth/login/2fa/", {"mfa_token": token, "code": code}, format="json")
        self.assertEqual((r.status_code, r.data["code"]), (403, "account_suspended"))

    @override_settings(TELEGRAM_BOT_TOKEN="123:SECRET", TELEGRAM_AUDIT_CHAT_ID="42")
    def test_telegram_message_mentions_response(self, _):
        sent = []
        with mock.patch("monitoring.telegram.send", side_effect=lambda text, chat: sent.append(text) or 1), \
                self.captureOnCommitCallbacks(execute=True):
            self.mass_view(self.doctor)
        self.assertIn("hisob siz ko'rib chiqmaguncha to'xtatildi", sent[0])


@mock.patch("rest_framework.throttling.ScopedRateThrottle.allow_request", return_value=True)
class DailyStatsTests(APITestCase):
    """Kunlik Telegram langari: oxirgi 24 soat statistikasi (faqat sonlar, shaxsiy ma'lumotsiz)."""

    PW = "Qx7#vTm2!pLz9w"

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.doctor = User.objects.create_user(username="st_doc", password=self.PW, role=Role.DOCTOR)
        self.pu = User.objects.create_user(username="st_pat", password=self.PW, role=Role.PATIENT)
        self.patient = Patient.objects.get(user=self.pu)

    def session_for(self, user):
        from rest_framework.test import APIRequestFactory
        from users import sessions
        from users.serializers import CustomTokenObtainPairSerializer
        sessions.start_session(APIRequestFactory().get("/"), user, CustomTokenObtainPairSerializer.get_token(user))

    def test_collects_last_24h(self, _):
        from django.utils import timezone
        from monitoring.stats import collect
        from users.security import MAX_FAILED_ATTEMPTS
        self.session_for(self.doctor)
        self.session_for(self.pu)
        self.session_for(self.pu)
        for _i in range(MAX_FAILED_ATTEMPTS):
            self.client.post("/api/auth/login/", {"username": "st_pat", "password": "wrong"}, format="json")
        log_audit(self.doctor, "view", patient=self.patient)
        log_audit(self.doctor, "export", patient=self.patient)
        now = timezone.now()
        SecurityAlert.objects.create(rule="mass_access", severity="high", actor=self.doctor, detail="x",
                                     first_seen=now, last_seen=now, auto_response="locked")
        old = now - timedelta(days=2)
        SecurityAlert.objects.create(rule="off_hours", severity="medium", actor=self.doctor, detail="eski",
                                     first_seen=old, last_seen=old, status="acknowledged")
        s = collect()
        self.assertEqual((s["logins"], s["logins_staff"], s["logins_patient"], s["login_users"]), (3, 1, 2, 2))
        self.assertEqual((s["login_failed"], s["lockout"]), (MAX_FAILED_ATTEMPTS, 1))
        self.assertEqual((s["alerts"], s["alerts_high"], s["suspended"]), (1, 1, 1))     # eskisi kirmaydi
        self.assertEqual((s["audit_view"], s["audit_export"]), (1, 1))

    def test_old_counter_buckets_are_ignored(self, _):
        from django.utils import timezone
        from monitoring.stats import bump, counters
        with mock.patch("monitoring.stats.timezone.now", return_value=timezone.now() - timedelta(hours=25)):
            bump("lockout")
        bump("lockout")
        self.assertEqual(counters()["lockout"], 1)

    def test_anchor_message_has_stats_without_personal_data(self, _):
        from records.audit_integrity import run_check
        from records.audit_anchor import build_message
        self.session_for(self.doctor)
        log_audit(self.doctor, "view", patient=self.patient, detail="Karta: Maxfiy Bemor")
        text = build_message(run_check())
        self.assertIn("📊 <b>Oxirgi 24 soat</b>", text)
        self.assertIn("Kirishlar: 1 (1 xodim, 0 bemor; 1 foydalanuvchi)", text)
        self.assertIn("Audit yozuvlari: 1 (ko'rish 1", text)
        for secret in ("st_doc", "st_pat", "Maxfiy"):
            self.assertNotIn(secret, text)

    def test_redis_down_still_builds_message(self, _):
        from records.audit_integrity import run_check
        from records.audit_anchor import build_message
        with mock.patch("monitoring.stats.cache.get_many", side_effect=ConnectionError("redis")):
            text = build_message(run_check())
        self.assertIn("Noto'g'ri parol: 0", text)

    def test_stats_failure_never_blocks_anchor(self, _):
        from records.audit_integrity import run_check
        from records.audit_anchor import build_message
        log_audit(self.doctor, "view", patient=self.patient)
        with mock.patch("monitoring.stats.collect", side_effect=RuntimeError("boom")):
            text = build_message(run_check())
        self.assertIn("zanjir butun", text)
        self.assertNotIn("Oxirgi 24 soat", text)
