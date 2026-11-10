"""
Xavfsizlik hodisalarini administratorning Telegram'iga real vaqtda yuborish.

Yuboriladigan hodisalar:
  * har bir yangi SecurityAlert (ko'p bemor kartasi, tungi murojaat, ko'p eksport, favqulodda kirish);
    mavjud ogohlantirishning hisoblagichi oshganda qayta yuborilmaydi;
  * login bloklanishi (5 marta noto'g'ri parol) - sayt va Django admin;
  * 2FA kodi 5 marta noto'g'ri - parol allaqachon to'g'ri kiritilgan, ya'ni parol oshkor bo'lgan bo'lishi mumkin;
  * zaxira kod bilan kirish.

Audit zanjiri buzilishi bu yerdan yuborilmaydi: kunlik langar vazifasi o'zi 🚨 xabar yuboradi.

Himoya:
  * foydalanuvchi kiritgan har qanday matn (login, sabab) HTML-escape qilinadi - xabarni soxtalashtirib bo'lmaydi;
  * 10 daqiqada ko'pi bilan ALERT_RATE_LIMIT ta xabar; oshsa bitta "ko'p hodisa" xabari, qolganlari faqat logda
    (hujumchi bot orqali administratorni xabarlarga ko'mib yubora olmaydi);
  * yuborish Celery'da, tranzaksiya yakunlangach - so'rov tezligiga ta'sir qilmaydi;
  * Telegram yoki Redis ishlamasa asosiy so'rov buzilmaydi.
"""
import html
import logging
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import telegram

security_logger = logging.getLogger("security")

ALERT_RATE_LIMIT = 20
ALERT_RATE_WINDOW = timedelta(minutes=10)
SEVERITY_ICON = {"high": "🔴", "medium": "🟠", "low": "🟡"}


def is_configured():
    return bool(telegram.token() and telegram.alert_chat_id())


def esc(value, limit=120):
    return html.escape(str(value or "")[:limit])


def _now_local():
    tz = getattr(settings, "QUEUE_TIME_ZONE", "") or settings.TIME_ZONE
    return timezone.now().astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M")


def _alerts_link():
    base = getattr(settings, "FRONTEND_URL", "").rstrip("/")
    return f"\n{base}/admin/alerts" if base.startswith("https://") else ""


def _allowed():
    """10 daqiqalik oynada nechanchi xabar ekanini sanaydi. Redis ishlamasa (0) - ruxsat."""
    from users.security import _increment

    n = _increment("tg:alerts:window", ALERT_RATE_WINDOW)
    if n == ALERT_RATE_LIMIT + 1:
        return "flood"
    return "ok" if n <= ALERT_RATE_LIMIT else "drop"


def notify(text):
    """Xabarni navbatga qo'yadi. Hech qachon istisno ko'tarmaydi."""
    if not is_configured():
        return False
    try:
        state = _allowed()
        if state == "drop":
            security_logger.warning("TELEGRAM-ALERT cheklandi (ko'p hodisa), yuborilmadi")
            return False
        if state == "flood":
            text = (f"⚠️ <b>ShifoSafe: juda ko'p xavfsizlik hodisasi</b>\n"
                    f"{int(ALERT_RATE_WINDOW.total_seconds() // 60)} daqiqada {ALERT_RATE_LIMIT} tadan ko'p. "
                    f"Keyingilari vaqtincha faqat admin panelda.{_alerts_link()}")
        from .tasks import send_security_alert

        transaction.on_commit(lambda: _enqueue(send_security_alert, text))
        return True
    except Exception:  # noqa: BLE001
        security_logger.exception("TELEGRAM-ALERT navbatga qo'yilmadi")
        return False


def _enqueue(task, text):
    try:
        task.delay(text)
    except Exception:  # noqa: BLE001 - Celery/Redis ishlamasa so'rov buzilmasin
        security_logger.exception("TELEGRAM-ALERT navbatga qo'yilmadi (Celery)")


# --- Xabar matnlari ---

def alert_message(alert):
    actor = alert.actor.username if alert.actor else "—"
    role = f" ({esc(alert.actor_role, 20)})" if alert.actor_role else ""
    action = {
        alert.LOCKED: "\n⛔ Avtomatik javob: barcha sessiyalari yopildi, hisob siz ko'rib chiqmaguncha to'xtatildi.",
        alert.LOGOUT: "\n⛔ Avtomatik javob: barcha sessiyalari yopildi (qayta kirishi kerak).",
    }.get(alert.auto_response, "")
    return (
        f"{SEVERITY_ICON.get(alert.severity, '⚠️')} <b>ShifoSafe: {esc(alert.get_rule_display())}</b>\n"
        f"Xodim: {esc(actor, 64)}{role}\n"
        f"{esc(alert.detail, 255)}{action}\n"
        f"{_now_local()}{_alerts_link()}"
    )


def _account_hint(username):
    """Login mavjudmi va kimga tegishli - administrator uchun (javobda foydalanuvchiga ko'rsatilmaydi)."""
    from users.models import User

    user = User.objects.filter(username__iexact=username or "").only("role", "is_active").first()
    if user is None:
        return "hisob mavjud emas (login taxmin qilinmoqda)"
    return f"hisob mavjud, rol: {esc(user.role, 20)}" + ("" if user.is_active else ", nofaol")


def login_locked(username, ident, minutes, source="sayt"):
    return notify(
        f"🟠 <b>ShifoSafe: login bloklandi</b> ({esc(source, 20)})\n"
        f"Login: <code>{esc(username, 64)}</code> — {_account_hint(username)}\n"
        f"IP: <code>{esc(ident, 64)}</code>\n"
        f"5 marta noto'g'ri urinish, {minutes} daqiqaga bloklandi\n"
        f"{_now_local()}"
    )


def mfa_exhausted(user, ident):
    return notify(
        f"🔴 <b>ShifoSafe: 2FA kodi 5 marta noto'g'ri</b>\n"
        f"Hisob: <code>{esc(user.username, 64)}</code> ({esc(user.role, 20)})\n"
        f"IP: <code>{esc(ident, 64)}</code>\n"
        f"Parol to'g'ri kiritilgan - u oshkor bo'lgan bo'lishi mumkin. Parolni almashtirish tavsiya etiladi.\n"
        f"{_now_local()}"
    )


def backup_code_login(user, ident, remaining):
    return notify(
        f"🟡 <b>ShifoSafe: zaxira kod bilan kirildi</b>\n"
        f"Hisob: <code>{esc(user.username, 64)}</code> ({esc(user.role, 20)})\n"
        f"IP: <code>{esc(ident, 64)}</code> · qolgan kodlar: {remaining}\n"
        f"Agar bu siz bo'lmasangiz - hisobni darhol bloklang.\n"
        f"{_now_local()}"
    )


def staff_email_changed(admin, user, old_masked, new_masked):
    """Xodim emaili almashtirildi: yangi emailga parol tiklash xati borishi mumkin - hisobni egallash belgisi."""
    return notify(
        f"🟠 <b>ShifoSafe: xodim emaili o'zgartirildi</b>\n"
        f"Hisob: <code>{esc(user.username, 64)}</code> ({esc(user.role, 20)})\n"
        f"Kim o'zgartirdi: <code>{esc(admin.username, 64)}</code>\n"
        f"{esc(old_masked, 120)} → {esc(new_masked, 120)}\n"
        f"Agar bu rejalashtirilmagan bo'lsa - hisobni tekshiring.\n"
        f"{_now_local()}"
    )
