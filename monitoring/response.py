"""
Shubhali faollikka avtomatik javob (STRIDE I3: "aniqlaydi, lekin to'xtatmaydi" kamchiligi).

Yuqori darajali ogohlantirish (qisqa vaqtda ko'p bemor kartasi yoki ko'p eksport) chiqqanda:
  * xodimning barcha sessiyalari darhol yopiladi - qo'lidagi access token ham keyingi so'rovda rad etiladi;
  * hisob administrator ogohlantirishni ko'rib chiqquncha to'xtatiladi: to'g'ri parol va 2FA bilan ham
    kira olmaydi ("Ko'rib chiqildi" bosilganda o'zi tiklanadi);
  * administratorga Telegram orqali xabar boradi (monitoring/notify.py).

Administrator hisobi to'xtatilmaydi, faqat sessiyalari yopiladi (qayta parol + 2FA bilan kirishi kerak):
yagona administrator o'zini bloklab qo'ysa, ogohlantirishni ko'rib chiqadigan odam qolmaydi.
Chegaralar va qoidalar: settings.SECURITY_ALERTS ("AUTO_SUSPEND_RULES": () - o'chirish).
"""
import logging

from django.utils.translation import gettext_lazy as _

from .detector import config

security_logger = logging.getLogger("security")

# Javob tili - interfeys tili (locale/uz, locale/ru)
SUSPENDED_MESSAGE = _("Your account has been temporarily suspended for a security review. "
                      "Please contact the administrator.")


def planned_response(rule, severity, actor):
    from .models import SecurityAlert

    if severity != "high" or rule not in tuple(config().get("AUTO_SUSPEND_RULES") or ()):
        return ""
    return SecurityAlert.LOGOUT if actor.role == "admin" else SecurityAlert.LOCKED


def apply_response(alert):
    """Ogohlantirish yaratilgandan keyin: sessiyalarni yopish va auditga yozish."""
    if not alert.auto_response or alert.actor is None:
        return
    from records.audit import log_audit
    from users.security import revoke_refresh_tokens
    from users.sessions import active_sessions, revoke

    user = alert.actor
    closed = revoke(active_sessions(user))
    revoke_refresh_tokens(user)
    what = "hisob to'xtatildi" if alert.auto_response == alert.LOCKED else "sessiyalar yopildi"
    log_audit(None, "update", detail=f"Avtomatik javob: {user.username} - {what} ({closed} ta sessiya, "
                                     f"ogohlantirish #{alert.id})"[:255])
    security_logger.warning("AVTOMATIK JAVOB | %s: %s, %s ta sessiya yopildi (ogohlantirish #%s)",
                            user.username, what, closed, alert.id)


def is_suspended(user):
    from .models import SecurityAlert

    return bool(user and user.pk) and SecurityAlert.objects.filter(
        actor=user, status=SecurityAlert.OPEN, auto_response=SecurityAlert.LOCKED).exists()
