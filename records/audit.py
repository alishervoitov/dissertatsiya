import logging

from .middleware import get_client_ip, get_current_request
from .models import AuditLog

security_logger = logging.getLogger("security")


def log_audit(actor, action, patient=None, medical_record=None, detail=""):
    """
    Har bir tibbiy ma'lumotga kirish/o'zgartirishni AuditLog jadvaliga yozadi.
    IP manzil va user-agent joriy so'rovdan (middleware orqali) olinadi.
    """
    request = get_current_request()
    ip = get_client_ip(request) if request else None
    user_agent = request.META.get("HTTP_USER_AGENT", "")[:300] if request else ""

    AuditLog.objects.create(
        actor=actor,
        action=action,
        patient=patient,
        medical_record=medical_record,
        detail=detail,
        ip_address=ip,
        user_agent=user_agent,
    )
    security_logger.info(
        "AUDIT | actor=%s action=%s patient=%s record=%s ip=%s detail=%s",
        getattr(actor, "username", "anon"), action,
        getattr(patient, "id", None), getattr(medical_record, "id", None),
        ip, detail,
    )