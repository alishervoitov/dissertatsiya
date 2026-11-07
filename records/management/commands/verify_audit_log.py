from django.core.management.base import BaseCommand

from records.audit_anchor import AnchorSendError, build_message, is_configured, send
from records.audit_integrity import run_check


class Command(BaseCommand):
    help = ("Audit jurnali xesh zanjirini tekshiradi (--checkpoint: butun bo'lsa nazorat nuqtasi qo'yadi; "
            "--telegram: natijani darhol Telegram'ga langar sifatida yuboradi).")

    def add_arguments(self, parser):
        parser.add_argument("--checkpoint", action="store_true")
        parser.add_argument("--telegram", action="store_true")

    def handle(self, *args, **opts):
        r = run_check(make_checkpoint=opts["checkpoint"])
        if r["ok"]:
            self.stdout.write(self.style.SUCCESS(
                f"Zanjir butun: {r['checked']} ta yozuv, oxirgi #{r['last_id']}, xesh {r['head'][:16]}..."))
        else:
            self.stdout.write(self.style.ERROR(f"ZANJIR BUZILGAN: {len(r['problems'])} ta muammo"))
            for p in r["problems"]:
                self.stdout.write(f"  {p['type']:9} #{p['id']}")

        if opts["telegram"]:
            if not is_configured():
                self.stdout.write(self.style.WARNING("Telegram sozlanmagan (TELEGRAM_BOT_TOKEN, TELEGRAM_AUDIT_CHAT_ID)"))
                return
            try:
                mid = send(build_message(r))
            except AnchorSendError as e:
                self.stdout.write(self.style.ERROR(f"Telegram'ga yuborilmadi: {e}"))
                return
            self.stdout.write(self.style.SUCCESS(f"Telegram'ga yuborildi (message_id={mid})"))
