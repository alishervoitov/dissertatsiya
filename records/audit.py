import logging

from .models import AuditLog

security_logger = logging.getLogger("security")


def log_audit(actor, action, patient=None, medical_record=None, detail=""):
    """
    Har bir tibbiy ma'lumotga kirish/o'zgartirishni AuditLog jadvaliga yozadi.
    """
    AuditLog.objects.create(
        actor=actor,
        action=action,
        patient=patient,
        medical_record=medical_record,
        detail=detail,
    )
    security_logger.info(
        "AUDIT | actor=%s action=%s patient=%s record=%s detail=%s",
        getattr(actor, "username", "anon"), action,
        getattr(patient, "id", None), getattr(medical_record, "id", None),
        detail,
    )