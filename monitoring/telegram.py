"""
Telegram orqali administratorga xabar yuborish (@ShifoSafeBot).

Ikki foydalanuvchi bor:
  * records/audit_anchor.py - kunlik audit langari (TELEGRAM_AUDIT_CHAT_ID);
  * monitoring/notify.py    - xavfsizlik ogohlantirishlari real vaqtda (TELEGRAM_ALERT_CHAT_ID,
    bo'sh bo'lsa audit chatiga).

Token faqat .env.docker'da; xato matnlarida va loglarda hech qachon chiqmaydi.
"""
import json
import urllib.error
import urllib.request

from django.conf import settings

API = "https://api.telegram.org/bot{token}/sendMessage"
TIMEOUT = 15


class TelegramError(Exception):
    """Yuborilmadi (tarmoq yoki API xatosi). Matnda token bo'lmaydi."""


def token():
    return getattr(settings, "TELEGRAM_BOT_TOKEN", "")


def alert_chat_id():
    return getattr(settings, "TELEGRAM_ALERT_CHAT_ID", "") or getattr(settings, "TELEGRAM_AUDIT_CHAT_ID", "")


def send(text, chat_id):
    """Xabarni yuboradi (HTML) va Telegram message_id ni qaytaradi."""
    if not (token() and chat_id):
        raise TelegramError("TELEGRAM_BOT_TOKEN yoki chat ID sozlanmagan")
    body = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode()
    req = urllib.request.Request(
        API.format(token=token()), data=body, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # nosec B310 - doimiy https manzil
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        # Telegram xato matnini (description) olamiz, URL'ni (tokenni) emas
        try:
            desc = json.loads(e.read().decode()).get("description", "")
        except Exception:  # noqa: BLE001
            desc = ""
        raise TelegramError(f"Telegram HTTP {e.code}: {desc}"[:200]) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise TelegramError(f"Tarmoq xatosi: {type(e).__name__}") from None
    if not data.get("ok"):
        raise TelegramError(f"Telegram rad etdi: {data.get('description', '')}"[:200])
    return data["result"]["message_id"]
