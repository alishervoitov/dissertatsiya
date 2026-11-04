"""Bemorlarning avatarini karta rasmiga (3x4, shifrlangan) ko'chirish - bir martalik.

Karta rasmi qo'shilgunga qadar bemor kabinetidagi 3x4 ramka avatar edi. Bu buyruq avatari bor,
lekin karta rasmi yo'q bemorlar uchun avatarni markazdan 3:4 qilib kesadi, metadatasini tozalaydi,
shifrlab PatientPhoto ga yozadi. Avatarning o'zi o'zgarmaydi. Mavjud karta rasmlari ustidan yozilmaydi.

    docker compose -f docker-compose.prod.yml exec backend python manage.py import_avatar_photos --dry-run
    docker compose -f docker-compose.prod.yml exec backend python manage.py import_avatar_photos
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from records.audit import log_audit
from records.models import Patient, PatientPhoto
from records.photo import PhotoError, encode, process_photo


class Command(BaseCommand):
    help = "Avatari bor, karta rasmi yo'q bemorlarning avatarini karta rasmiga ko'chiradi."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Faqat hisobot, hech narsa yozilmaydi.")

    def handle(self, *args, dry_run=False, **opts):
        candidates = (Patient.objects.select_related("user")
                      .exclude(user__avatar="").exclude(user__avatar__isnull=True)
                      .filter(photo__isnull=True))
        done = skipped = 0
        for patient in candidates:
            avatar = patient.user.avatar
            who = patient.user.username
            try:
                with avatar.open("rb") as fh:
                    data = process_photo(fh)
            except (PhotoError, OSError, ValueError) as exc:
                skipped += 1
                self.stdout.write(self.style.WARNING(f"  o'tkazib yuborildi: {who} ({exc})"))
                continue
            if not dry_run:
                PatientPhoto.objects.create(patient=patient, image=encode(data), uploaded_by=None,
                                            updated_at=timezone.now())
                log_audit(None, "update", patient=patient,
                          detail="Bemor rasmi (3x4) avatardan ko'chirildi (import_avatar_photos)")
            done += 1
            self.stdout.write(f"  {'ko`chiriladi' if dry_run else 'ko`chirildi'}: {who}")
        verb = "Ko'chiriladi" if dry_run else "Ko'chirildi"
        self.stdout.write(self.style.SUCCESS(f"{verb}: {done} ta bemor, o'tkazib yuborildi: {skipped} ta."))
        if dry_run:
            self.stdout.write("Haqiqatda bajarish uchun --dry-run ni olib tashlang.")
