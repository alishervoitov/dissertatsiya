from django.core.management.base import BaseCommand

from records.audit_integrity import run_check


class Command(BaseCommand):
    help = "Audit jurnali xesh zanjirini tekshiradi (--checkpoint: butun bo'lsa nazorat nuqtasi qo'yadi)."

    def add_arguments(self, parser):
        parser.add_argument("--checkpoint", action="store_true")

    def handle(self, *args, **opts):
        r = run_check(make_checkpoint=opts["checkpoint"])
        if r["ok"]:
            self.stdout.write(self.style.SUCCESS(
                f"Zanjir butun: {r['checked']} ta yozuv, oxirgi #{r['last_id']}, xesh {r['head'][:16]}..."))
        else:
            self.stdout.write(self.style.ERROR(f"ZANJIR BUZILGAN: {len(r['problems'])} ta muammo"))
            for p in r["problems"]:
                self.stdout.write(f"  {p['type']:9} #{p['id']}")
