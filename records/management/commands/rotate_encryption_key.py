"""
Shifrlash kalitini almashtirish (key rotation) - eski ma'lumot yo'qolmaydi.

Tartib:
  1) Yangi kalit yaratiladi va .env.docker da BIRINCHI qilib yoziladi:
         FIELD_ENCRYPTION_KEY=YANGI_KALIT,ESKI_KALIT
     Backend qayta ishga tushirilgach yangi yozuvlar YANGI kalit bilan shifrlanadi,
     eskilari esa ESKI kalit bilan o'qilaveradi (MultiFernet).
  2) python manage.py rotate_encryption_key --check   - qaysi kalit bilan nechta qiymat
  3) python manage.py rotate_encryption_key           - barcha qiymatlar YANGI kalitga o'tkaziladi
  4) --check yana: eski kalitda 0 ta qolgani ko'rinadi -> ESKI_KALIT .env dan olib tashlanadi.

Xavfsizlik:
  * bitta tranzaksiya - xato bo'lsa hech narsa o'zgarmaydi;
  * birorta qiymat birorta kalit bilan ham ochilmasa, almashtirish boshlanmaydi
    (aks holda o'sha ma'lumot butunlay yo'qolishi mumkin edi);
  * qiymatlar to'g'ridan-to'g'ri SQL bilan o'qiladi/yoziladi (model maydoni qayta shifrlamasin).
"""
import logging
from collections import Counter

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from records.encryption import encrypted_fields, key_list

security_logger = logging.getLogger("security")


def _which_key(fernets, token):
    """Qiymatni qaysi kalit ochadi: 0 - asosiy, 1.. - eskilari, None - hech qaysi."""
    for i, f in enumerate(fernets):
        try:
            f.decrypt(token)
            return i
        except InvalidToken:
            continue
    return None


class Command(BaseCommand):
    help = "Shifrlangan maydonlarni FIELD_ENCRYPTION_KEY dagi birinchi (yangi) kalitga o'tkazadi."

    def add_arguments(self, parser):
        parser.add_argument("--check", action="store_true", help="Faqat hisobot: o'zgartirmaydi.")
        parser.add_argument("--batch", type=int, default=500)

    def _scan(self, fernets):
        qn = connection.ops.quote_name
        stats = Counter()
        broken = []
        plan = []
        for model, fields in encrypted_fields():
            table = model._meta.db_table
            pk = model._meta.pk.column
            cols = [f.column for f in fields]
            with connection.cursor() as cur:
                cur.execute(f"SELECT {qn(pk)}, {', '.join(qn(c) for c in cols)} FROM {qn(table)} ORDER BY {qn(pk)}")
                for row in cur.fetchall():
                    for col, value in zip(cols, row[1:]):
                        if not value:
                            continue
                        idx = _which_key(fernets, value.encode())
                        if idx is None:
                            stats["broken"] += 1
                            broken.append(f"{table}.{col} #{row[0]}")
                        else:
                            stats[idx] += 1
                            if idx != 0:
                                plan.append((table, pk, col, row[0], value))
        return stats, broken, plan

    def _report(self, stats, nkeys):
        self.stdout.write(f"  asosiy (yangi) kalit:  {stats.get(0, 0)} ta qiymat")
        for i in range(1, nkeys):
            self.stdout.write(f"  eski kalit #{i}:         {stats.get(i, 0)} ta qiymat")
        if stats.get("broken"):
            self.stdout.write(self.style.ERROR(f"  ochilmaydigan:         {stats['broken']} ta qiymat"))

    def handle(self, *args, **opts):
        keys = key_list()
        try:
            fernets = [Fernet(k.encode()) for k in keys]
        except (ValueError, TypeError) as exc:
            raise CommandError(f"FIELD_ENCRYPTION_KEY dagi kalitlardan biri noto'g'ri formatda: {exc}")
        if len(set(keys)) != len(keys):
            raise CommandError("FIELD_ENCRYPTION_KEY da bir xil kalit ikki marta yozilgan.")

        stats, broken, plan = self._scan(fernets)
        self.stdout.write(f"Kalitlar soni: {len(keys)}. Shifrlangan qiymatlar:")
        self._report(stats, len(keys))

        if opts["check"]:
            old_left = sum(v for k, v in stats.items() if k not in (0, "broken"))
            if old_left == 0 and not broken and len(keys) > 1:
                self.stdout.write(self.style.SUCCESS(
                    "Eski kalit(lar) bilan qiymat qolmadi - ularni FIELD_ENCRYPTION_KEY dan olib tashlash mumkin."))
            return

        if broken:
            for b in broken[:20]:
                self.stdout.write(f"    {b}")
            raise CommandError("Ba'zi qiymatlarni hech qaysi kalit ochmayapti - almashtirish to'xtatildi. "
                               "Avval yo'qolgan eski kalitni FIELD_ENCRYPTION_KEY ga qo'shing.")
        if not plan:
            self.stdout.write(self.style.SUCCESS("Barcha qiymatlar allaqachon asosiy kalit bilan - ish yo'q."))
            return

        multi = MultiFernet(fernets)
        qn = connection.ops.quote_name
        with transaction.atomic():
            with connection.cursor() as cur:
                for table, pk, col, row_id, value in plan:
                    # rotate: eski kalit bilan ochadi, asosiy kalit bilan qayta shifrlaydi (vaqt belgisi saqlanadi)
                    new_value = multi.rotate(value.encode()).decode()
                    cur.execute(f"UPDATE {qn(table)} SET {qn(col)} = %s WHERE {qn(pk)} = %s", [new_value, row_id])
            from records.audit import log_audit
            log_audit(None, "update", detail=f"Shifrlash kaliti almashtirildi: {len(plan)} ta qiymat yangi kalitga o'tkazildi")

        security_logger.warning("Shifrlash kaliti almashtirildi: %s ta qiymat qayta shifrlandi", len(plan))
        stats, broken, _ = self._scan(fernets)
        self.stdout.write(self.style.SUCCESS(f"Tayyor: {len(plan)} ta qiymat yangi kalitga o'tkazildi. Hozirgi holat:"))
        self._report(stats, len(keys))
        self.stdout.write("Endi eski kalitni FIELD_ENCRYPTION_KEY dan olib tashlab, backendni qayta ishga tushiring.")
