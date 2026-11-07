"""
Audit zanjirini tashqi guvohga "langarlash" (T1 qoldiq xavfi).

Muammo: HMAC kaliti (AUDIT_HMAC_KEY) shu serverda turadi. Serverni to'liq egallagan buzg'unchi
yozuvlarni o'zgartirib, butun zanjirni kalit bilan qayta hisoblasa, ichki tekshiruv buni sezmaydi.

Yechim: har kuni zanjir oxiri (oxirgi id, uning xeshi, yozuvlar soni) server nazoratidan tashqariga -
administratorning Telegram chatiga yuboriladi. Keyin istalgan paytda shu qiymatlar bilan solishtiriladi
(manage.py verify_audit_anchor). Langargacha bo'lgan biror yozuv o'zgartirilsa yoki o'chirilsa, zanjir
qayta hisoblangan bo'lsa ham shu nuqtadagi xesh boshqacha chiqadi.

Xabarda bemor ma'lumoti yo'q: faqat sana, sonlar va xesh (xeshdan hech narsa tiklanmaydi).
"""
import hmac
import json
import logging
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

from django.conf import settings

from . import audit_chain

security_logger = logging.getLogger("security")

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
TIMEOUT = 15


class AnchorSendError(Exception):
    """Telegram'ga yuborilmadi (tarmoq yoki API xatosi). Matnda token bo'lmaydi."""


def is_configured():
    return bool(getattr(settings, "TELEGRAM_BOT_TOKEN", "") and getattr(settings, "TELEGRAM_AUDIT_CHAT_ID", ""))


def _local(dt):
    # Baza UTC'da; xabar administrator uchun - mahalliy vaqtda (QUEUE_TIME_ZONE, standart Asia/Tashkent)
    tz = getattr(settings, "QUEUE_TIME_ZONE", "") or settings.TIME_ZONE
    return dt.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M")


def build_message(result):
    """run_check() natijasidan Telegram xabari (HTML)."""
    when = _local(result["checked_at"])
    if result["ok"]:
        if not result["last_id"]:
            return f"🛡 <b>ShifoSafe audit langari</b>\n{when}\nJurnal hali bo'sh."
        return (
            f"🛡 <b>ShifoSafe audit langari</b>\n"
            f"{when} · ✅ zanjir butun\n"
            f"Yozuvlar: {result['checked']} · oxirgi #{result['last_id']}\n"
            f"Xesh: <code>{result['head']}</code>\n\n"
            f"Tekshirish:\n<code>python manage.py verify_audit_anchor {result['last_id']} "
            f"{result['head']} --count {result['checked']}</code>"
        )
    ids = ", ".join(f"{p['type']} #{p['id']}" for p in result["problems"][:5])
    return (
        f"🚨 <b>ShifoSafe: AUDIT ZANJIRI BUZILGAN</b>\n"
        f"{when}\n"
        f"Muammolar: {len(result['problems'])} ta ({ids})\n"
        f"Langar yuborilmadi. Serverni tekshiring."
    )


def send(text):
    """Xabarni yuboradi va Telegram message_id ni qaytaradi. Token hech qachon logga tushmaydi."""
    if not is_configured():
        raise AnchorSendError("TELEGRAM_BOT_TOKEN yoki TELEGRAM_AUDIT_CHAT_ID sozlanmagan")
    body = json.dumps({
        "chat_id": settings.TELEGRAM_AUDIT_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode()
    req = urllib.request.Request(
        TELEGRAM_API.format(token=settings.TELEGRAM_BOT_TOKEN), data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # nosec B310
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        # Telegram xato matnini (description) olamiz, URL'ni (tokenni) emas
        try:
            desc = json.loads(e.read().decode()).get("description", "")
        except Exception:  # noqa: BLE001
            desc = ""
        raise AnchorSendError(f"Telegram HTTP {e.code}: {desc}"[:200]) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise AnchorSendError(f"Tarmoq xatosi: {type(e).__name__}") from None
    if not data.get("ok"):
        raise AnchorSendError(f"Telegram rad etdi: {data.get('description', '')}"[:200])
    return data["result"]["message_id"]


def check_anchor(model, last_id, expected_hash, expected_count=None, key=None):
    """
    Tashqi langar bilan solishtirish: 1..last_id oralig'idagi zanjir boshidan qayta hisoblanadi.
    Natija: ok va sabablar ro'yxati.
    """
    problems = []
    prev = audit_chain.GENESIS
    count = 0
    found = None
    for entry in model.objects.filter(id__lte=last_id).order_by("id").iterator(chunk_size=2000):
        count += 1
        if entry.prev_hash != prev:
            problems.append(f"#{entry.id}: oldingi yozuv bilan bog'lanish uzilgan")
        if not hmac.compare_digest(audit_chain.compute_hash(entry, entry.prev_hash, key), entry.entry_hash or ""):
            problems.append(f"#{entry.id}: mazmuni o'zgartirilgan")
        prev = entry.entry_hash
        if entry.id == last_id:
            found = entry
    if found is None:
        problems.append(f"#{last_id}: langar yozuvi bazada yo'q (o'chirilgan)")
    elif not hmac.compare_digest(found.entry_hash, expected_hash.strip().lower()):
        problems.append(f"#{last_id}: xesh langardagidan farq qiladi (zanjir qayta yozilgan)")
    if expected_count is not None and count != expected_count:
        problems.append(f"yozuvlar soni {count}, langarda {expected_count} (yozuv o'chirilgan yoki qo'shilgan)")
    return {"ok": not problems, "checked": count, "problems": problems[:50]}
