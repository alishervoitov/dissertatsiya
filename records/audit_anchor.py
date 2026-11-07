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
import logging
from zoneinfo import ZoneInfo

from django.conf import settings

from monitoring import telegram

from . import audit_chain

security_logger = logging.getLogger("security")

# Eski nom saqlanadi (tasks.py va buyruqlar shu nom bilan ushlaydi)
AnchorSendError = telegram.TelegramError


def is_configured():
    return bool(telegram.token() and getattr(settings, "TELEGRAM_AUDIT_CHAT_ID", ""))


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
    """Langarni audit chatiga yuboradi va Telegram message_id ni qaytaradi."""
    return telegram.send(text, getattr(settings, "TELEGRAM_AUDIT_CHAT_ID", ""))


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
