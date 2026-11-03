"""
Audit jurnalining yaxlitligi: HMAC xesh zanjiri.

Har bir yozuv o'zidan oldingi yozuvning xeshini (prev_hash) saqlaydi, o'zining xeshi
(entry_hash) esa yozuv maydonlari + prev_hash dan HMAC-SHA256 bilan hisoblanadi:

    entry_hash[i] = HMAC(K, maydonlar[i] || entry_hash[i-1])

Natija:
  * bitta yozuvdagi har qanday o'zgarish uning xeshini buzadi ("modified");
  * o'rtadan yozuv o'chirilsa yoki qo'shilsa, keyingi yozuvning prev_hash'i mos kelmaydi ("link");
  * oxiridan yozuvlar o'chirilsa, kunlik nazorat nuqtasi (AuditCheckpoint) buni ko'rsatadi ("truncated").

Kalit K bazada saqlanmaydi (AUDIT_HMAC_KEY yoki SECRET_KEY dan olinadi). Shuning uchun faqat
bazaga kirish huquqi bor buzg'unchi ham zanjirni "qayta hisoblab" izini yashira olmaydi.
"""
import hashlib
import hmac
import ipaddress
import json
from datetime import timezone as dt_timezone

from django.conf import settings
from django.db import connection

GENESIS = "0" * 64
CHAIN_VERSION = 1
# pg_advisory_xact_lock uchun ixtiyoriy doimiy son: bir vaqtda faqat bitta yozuv zanjirga ulanadi
LOCK_ID = 7_310_204_611


def secret_key_derived():
    """AUDIT_HMAC_KEY qo'yilmaganda ishlatiladigan kalit (SECRET_KEY dan)."""
    return hashlib.sha256(f"audit-chain:{settings.SECRET_KEY}".encode()).digest()


def _key():
    explicit = getattr(settings, "AUDIT_HMAC_KEY", "")
    if explicit:
        return explicit.encode()
    return secret_key_derived()


def actor_snapshot(user):
    """Kim qilganini yozuv ichida saqlaymiz: foydalanuvchi keyin o'chirilsa ham iz qoladi."""
    if user is None:
        return ""
    return f"{user.pk}:{user.username}:{getattr(user, 'role', '')}"[:150]


def _norm_ip(ip):
    if not ip:
        return ""
    try:
        return ipaddress.ip_address(str(ip)).compressed
    except ValueError:
        return str(ip)


def payload(entry, prev_hash):
    ts = entry.timestamp.astimezone(dt_timezone.utc).isoformat(timespec="microseconds")
    data = [
        CHAIN_VERSION, entry.action, entry.actor_snapshot, entry.patient_ref, entry.record_ref,
        entry.detail or "", _norm_ip(entry.ip_address), entry.user_agent or "", ts, prev_hash,
    ]
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()


def compute_hash(entry, prev_hash, key=None):
    return hmac.new(key or _key(), payload(entry, prev_hash), hashlib.sha256).hexdigest()


def lock_chain():
    """Postgres'da tranzaksiya oxirigacha qulf: ikki parallel so'rov bir xil prev_hash olmasin."""
    if connection.vendor == "postgresql":
        with connection.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", [LOCK_ID])


def head_hash(model):
    last = model.objects.order_by("-id").values_list("entry_hash", flat=True).first()
    return last or GENESIS


def verify(model, checkpoint_model=None, limit=50, key=None):
    """Butun zanjirni boshidan tekshiradi. Natija: ok, tekshirilgan soni, oxirgi xesh, muammolar."""
    problems = []
    prev = GENESIS
    count = 0
    last_id = None

    def add(kind, entry_id, **extra):
        if len(problems) < limit:
            problems.append({"type": kind, "id": entry_id, **extra})

    for entry in model.objects.order_by("id").iterator(chunk_size=2000):
        count += 1
        if entry.prev_hash != prev:
            add("link", entry.id)           # oldingi yozuv(lar) o'chirilgan yoki joyi o'zgargan
        if not hmac.compare_digest(compute_hash(entry, entry.prev_hash, key), entry.entry_hash or ""):
            add("modified", entry.id)       # yozuv mazmuni o'zgartirilgan
        prev = entry.entry_hash
        last_id = entry.id

    if checkpoint_model is not None:
        for cp in checkpoint_model.objects.order_by("id"):
            row = model.objects.filter(pk=cp.last_id).values_list("entry_hash", flat=True).first()
            if row is None:
                add("truncated", cp.last_id, checkpoint=cp.id)
            elif row != cp.last_hash:
                add("modified", cp.last_id, checkpoint=cp.id)
            elif model.objects.filter(id__lte=cp.last_id).count() != cp.count:
                add("truncated", cp.last_id, checkpoint=cp.id)

    return {"ok": not problems, "checked": count, "last_id": last_id, "head": prev, "problems": problems}
