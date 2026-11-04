"""Pentest/sinov hisoblarini dalil sifatida arxivlab, bazadan o'chirish.

    # 1) Reja: hech narsa yozilmaydi va o'chirilmaydi
    python manage.py archive_test_accounts pentest_1792391093 avatartest
    # 2) Arxivlash + o'chirish
    python manage.py archive_test_accounts pentest_1792391093 avatartest --confirm --reason "Pentest 2026-10-19"

Arxiv (tar.gz, faqat egasi o'qiy oladi) EVIDENCE_DIR ga yoziladi (sukut: protected_media/evidence -
docker volume'da, konteyner qayta qurilsa ham saqlanadi). Ichida:
  * manifest.json - hisob ma'lumotlari, sessiyalar (IP, brauzer), audit yozuvlari (xeshlari bilan),
    xavfsizlik ogohlantirishlari va har bir faylning SHA-256 si;
  * files/<username>/... - yuklangan fayllar O'ZGARTIRILMAGAN holda (pentest yuklamalari ham).
Arxivning SHA-256 si chiqariladi va audit jurnaliga yoziladi (dalillar zanjiri - chain of custody).

Xavfsizlik cheklovlari:
  * hisoblar faqat aniq login bilan beriladi (andoza/prefiks yo'q - adashib boshqasini o'chirmaslik uchun);
  * administrator, shifokor, laborant va staff/superuser hisoblari o'chirilmaydi;
  * audit jurnali o'chirilmaydi: yozuvlarda faqat bog'lanish NULL bo'ladi, xeshga kiradigan
    login nusxasi (actor_snapshot) saqlanadi - zanjir butunligicha qoladi.
"""
import hashlib
import io
import json
import logging
import os
import tarfile
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from users.models import Role, User

security_logger = logging.getLogger("security")
DELETABLE_ROLES = {Role.PATIENT}


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _iso(dt):
    return dt.isoformat() if dt else None


def _read(field_file):
    try:
        with field_file.open("rb") as fh:
            return fh.read()
    except (OSError, ValueError):
        return None


class Command(BaseCommand):
    help = "Sinov/pentest hisoblarini dalil sifatida arxivlab, bazadan o'chiradi."

    def add_arguments(self, parser):
        parser.add_argument("usernames", nargs="+")
        parser.add_argument("--confirm", action="store_true", help="Haqiqatda arxivlash va o'chirish.")
        parser.add_argument("--reason", default="Pentest/sinov hisoblari", help="Arxivga yoziladigan sabab.")
        parser.add_argument("--out", default="", help="Arxiv papkasi (sukut: EVIDENCE_DIR).")

    # ---------- yig'ish ----------

    def _collect(self, user):
        from labs.models import LabAttachment, LabResult
        from monitoring.models import SecurityAlert
        from records.models import AuditLog, MedicalRecord, Patient, RecordAttachment

        patient = Patient.objects.filter(user=user).first()
        files = []
        if user.avatar:
            files.append(("avatar", user.avatar))
        if patient:
            for a in RecordAttachment.objects.filter(record__patient=patient):
                files.append(("record_attachment", a.file))
            for a in LabAttachment.objects.filter(result__patient=patient):
                files.append(("lab_attachment", a.file))

        audit_q = AuditLog.objects.filter(actor=user)
        if patient:
            audit_q = audit_q | AuditLog.objects.filter(patient=patient)
        audit = [{"id": e.id, "timestamp": _iso(e.timestamp), "action": e.action, "actor": e.actor_snapshot,
                  "patient_ref": e.patient_ref, "detail": e.detail, "ip": e.ip_address,
                  "user_agent": e.user_agent, "entry_hash": e.entry_hash}
                 for e in audit_q.distinct().order_by("id")]

        info = {
            "id": user.id, "username": user.username, "role": user.role, "email": user.email,
            "first_name": user.first_name, "last_name": user.last_name, "phone": user.phone,
            "date_joined": _iso(user.date_joined), "last_login": _iso(user.last_login),
            "is_active": user.is_active, "patient_id": patient.id if patient else None,
            "sessions": [{"ip": s.ip, "user_agent": s.user_agent, "created_at": _iso(s.created_at),
                          "last_seen": _iso(s.last_seen), "revoked_at": _iso(s.revoked_at)}
                         for s in user.login_sessions.all()],
            "security_alerts": [{"rule": a.rule, "severity": a.severity, "status": a.status, "count": a.count,
                                 "detail": a.detail, "first_seen": _iso(a.first_seen), "last_seen": _iso(a.last_seen)}
                                for a in SecurityAlert.objects.filter(actor=user)],
            "audit_entries": audit,
            "counts": {
                "medical_records": MedicalRecord.objects.filter(patient=patient).count() if patient else 0,
                "lab_results": LabResult.objects.filter(patient=patient).count() if patient else 0,
            },
            "files": [],
        }
        return patient, files, info

    # ---------- asosiy ----------

    def handle(self, *args, usernames, confirm, reason, out, **opts):
        users = []
        for name in dict.fromkeys(usernames):
            user = User.objects.filter(username__iexact=name).first()
            if not user:
                raise CommandError(f"Hisob topilmadi: {name}")
            if user.role not in DELETABLE_ROLES or user.is_staff or user.is_superuser:
                raise CommandError(f"{user.username}: rol '{user.role}' (yoki staff) - bu buyruq faqat bemor "
                                   f"rolidagi sinov hisoblarini o'chiradi.")
            users.append(user)

        collected = [(u, *self._collect(u)) for u in users]
        for user, patient, files, info in collected:
            self.stdout.write(f"  {user.username}: rol={user.role}, ro'yxatdan={info['date_joined'][:10]}, "
                              f"fayllar={len(files)}, sessiyalar={len(info['sessions'])}, "
                              f"audit yozuvlari={len(info['audit_entries'])}, "
                              f"tibbiy yozuvlar={info['counts']['medical_records']}")
        if not confirm:
            self.stdout.write(self.style.WARNING("Reja rejimi: hech narsa yozilmadi va o'chirilmadi. "
                                                 "Bajarish uchun --confirm qo'shing."))
            return

        # 1) Arxiv: fayllar o'zgartirilmagan holda + manifest
        out_dir = Path(out or getattr(settings, "EVIDENCE_DIR", settings.BASE_DIR / "protected_media" / "evidence"))
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = timezone.now().strftime("%Y%m%d-%H%M%S")
        archive_path = out_dir / f"test-accounts-{stamp}.tar.gz"
        manifest = {"created_at": _iso(timezone.now()), "reason": reason, "accounts": []}
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for user, patient, files, info in collected:
                for kind, field_file in files:
                    data = _read(field_file)
                    entry = {"kind": kind, "stored_name": field_file.name, "size": None, "sha256": None}
                    if data is not None:
                        arcname = f"files/{user.username}/{kind}__{os.path.basename(field_file.name)}"
                        ti = tarfile.TarInfo(arcname)
                        ti.size, ti.mtime, ti.mode = len(data), int(timezone.now().timestamp()), 0o400
                        tar.addfile(ti, io.BytesIO(data))
                        entry.update(size=len(data), sha256=_sha256(data), archive_path=arcname)
                    else:
                        entry["missing"] = True
                    info["files"].append(entry)
                manifest["accounts"].append(info)
            raw = json.dumps(manifest, ensure_ascii=False, indent=2, default=str).encode()
            ti = tarfile.TarInfo("manifest.json")
            ti.size, ti.mtime, ti.mode = len(raw), int(timezone.now().timestamp()), 0o400
            tar.addfile(ti, io.BytesIO(raw))
        blob = buf.getvalue()
        fd = os.open(archive_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(blob)
        digest = _sha256(blob)

        # 2) O'chirish (bitta tranzaksiyada); fayllar baza muvaffaqiyatli o'zgargandan keyin o'chiriladi
        from records.audit import log_audit

        names = [u.username for u in users]
        to_remove = [f for _, _, files, _ in collected for _, f in files]
        with transaction.atomic():
            for user in users:
                user.delete()
            log_audit(None, "delete", detail=f"Sinov hisoblari arxivlab o'chirildi: {', '.join(names)}"
                                             f" (arxiv sha256 {digest[:16]})"[:255])

            def remove_files():
                for f in to_remove:
                    try:
                        f.storage.delete(f.name)
                    except OSError:
                        pass

            transaction.on_commit(remove_files)

        security_logger.warning("Sinov hisoblari arxivlab o'chirildi: %s; arxiv %s sha256=%s",
                                ", ".join(names), archive_path, digest)
        self.stdout.write(self.style.SUCCESS(f"O'chirildi: {len(users)} ta hisob ({', '.join(names)})"))
        self.stdout.write(f"Arxiv:  {archive_path}")
        self.stdout.write(f"SHA-256: {digest}")
        self.stdout.write("Audit zanjirini tekshiring: python manage.py verify_audit_log")
