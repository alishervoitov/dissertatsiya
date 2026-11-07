"""
Audit zanjiri kalitini almashtirish (masalan, SECRET_KEY asosidagi kalitdan AUDIT_HMAC_KEY ga o'tish).

Xavfsiz tartib:
  1) zanjir ESKI kalit bilan to'liq tekshiriladi - buzilgan bo'lsa hech narsa o'zgartirilmaydi
     (aks holda yangi kalit buzilgan yozuvlarni "qonuniylashtirib" qo'ygan bo'lardi);
  2) bitta tranzaksiyada himoya triggeri vaqtincha o'chiriladi, barcha xeshlar YANGI kalit
     bilan qayta hisoblanadi, trigger qayta yoqiladi;
  3) eski nazorat nuqtalari o'chiriladi, yangisi qo'yiladi, voqea audit va xavfsizlik logiga yoziladi.

Ishlatish:
  python manage.py rekey_audit_chain --from-secret-key
"""
import logging

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from records import audit_chain
from records.models import AuditCheckpoint, AuditLog

security_logger = logging.getLogger("security")


class Command(BaseCommand):
    help = "Audit xesh zanjirini eski kalit bilan tekshirib, joriy AUDIT_HMAC_KEY bilan qayta hisoblaydi."

    def add_arguments(self, parser):
        parser.add_argument("--from-secret-key", action="store_true",
                            help="Eski kalit - SECRET_KEY dan olingan (AUDIT_HMAC_KEY qo'yilmagan paytdagi).")

    def handle(self, *args, **opts):
        if not opts["from_secret_key"]:
            raise CommandError("Eski kalit manbasini ko'rsating: --from-secret-key")
        if not getattr(settings, "AUDIT_HMAC_KEY", ""):
            raise CommandError("AUDIT_HMAC_KEY sozlanmagan - yangi kalit yo'q.")

        old_key = audit_chain.secret_key_derived()
        new_key = settings.AUDIT_HMAC_KEY.encode()
        if old_key == new_key:
            raise CommandError("Eski va yangi kalit bir xil.")

        # 1. Eski kalit bilan tekshiruv
        check = audit_chain.verify(AuditLog, AuditCheckpoint, key=old_key)
        if not check["ok"]:
            if audit_chain.verify(AuditLog, AuditCheckpoint, key=new_key)["ok"]:
                self.stdout.write(self.style.SUCCESS("Zanjir allaqachon yangi kalit bilan - hech narsa qilinmadi."))
                return
            for p in check["problems"]:
                self.stdout.write(f"  {p['type']:9} #{p['id']}")
            raise CommandError("Zanjir eski kalit bilan ham buzilgan - kalit almashtirilmadi.")

        # 2. Qayta hisoblash (bitta tranzaksiya: xato bo'lsa hammasi bekor bo'ladi)
        pg = connection.vendor == "postgresql"
        with transaction.atomic():
            audit_chain.lock_chain()
            with connection.cursor() as cur:
                if pg:
                    # Kechiktirilgan FK tekshiruvlari ALTER TABLE ni to'smasin ("pending trigger events")
                    cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
                    cur.execute("ALTER TABLE records_auditlog DISABLE TRIGGER records_auditlog_guard_row")
                prev = audit_chain.GENESIS
                count = 0
                for entry in AuditLog.objects.order_by("id").iterator(chunk_size=2000):
                    new_hash = audit_chain.compute_hash(entry, prev, new_key)
                    cur.execute("UPDATE records_auditlog SET prev_hash = %s, entry_hash = %s WHERE id = %s",
                                [prev, new_hash, entry.id])
                    prev = new_hash
                    count += 1
                if pg:
                    cur.execute("ALTER TABLE records_auditlog ENABLE TRIGGER records_auditlog_guard_row")
            AuditCheckpoint.objects.all().delete()

        # 3. Yangi kalit bilan tekshiruv, nazorat nuqtasi va qayd
        from records.audit import log_audit
        log_audit(None, "update", detail=f"Audit zanjiri kaliti almashtirildi ({count} ta yozuv qayta hisoblandi)")
        from records.audit_integrity import run_check
        result = run_check(make_checkpoint=True)
        security_logger.warning("AUDIT-CHAIN kaliti almashtirildi: %s ta yozuv, yangi oxirgi xesh %s", count, result["head"])
        if not result["ok"]:
            raise CommandError("Qayta hisoblashdan keyin tekshiruv o'tmadi!")
        self.stdout.write(self.style.SUCCESS(
            f"Tayyor: {count} ta yozuv yangi kalit bilan qayta hisoblandi, zanjir butun ({result['checked']} ta)."))
