from django.core.management.base import BaseCommand, CommandError

from records.audit_anchor import check_anchor
from records.models import AuditLog


class Command(BaseCommand):
    help = ("Audit zanjirini Telegram'ga yuborilgan langar bilan solishtiradi. "
            "Argumentlar langar xabaridagi buyruqdan nusxa olinadi.")

    def add_arguments(self, parser):
        parser.add_argument("last_id", type=int, help="Langardagi oxirgi yozuv raqami")
        parser.add_argument("hash", help="Langardagi xesh (64 belgi)")
        parser.add_argument("--count", type=int, default=None, help="Langardagi yozuvlar soni")

    def handle(self, *args, **opts):
        h = opts["hash"].strip().lower()
        if len(h) != 64 or any(c not in "0123456789abcdef" for c in h):
            raise CommandError("Xesh 64 ta hex belgidan iborat bo'lishi kerak")
        r = check_anchor(AuditLog, opts["last_id"], h, opts["count"])
        if r["ok"]:
            self.stdout.write(self.style.SUCCESS(
                f"Langar mos: #{opts['last_id']} gacha {r['checked']} ta yozuv o'zgartirilmagan."))
            return
        self.stdout.write(self.style.ERROR(f"LANGAR MOS EMAS: {len(r['problems'])} ta muammo"))
        for p in r["problems"]:
            self.stdout.write(f"  {p}")
        raise SystemExit(1)
