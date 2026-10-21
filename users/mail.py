"""
SMTP sozlanmaganda ishlatiladigan email backend (log bandi).

Avvalgi console backend xat matnini - parolni tiklash tokeni bilan - worker logiga yozardi.
Bu backend xatni yubormaydi va matnini yozmaydi: faqat niqoblangan qabul qiluvchi va mavzu.
"""
import logging

from django.core.mail.backends.base import BaseEmailBackend

from .security import mask_email

security_logger = logging.getLogger("security")


class RedactingEmailBackend(BaseEmailBackend):
    def send_messages(self, email_messages):
        for message in email_messages:
            security_logger.warning(
                "Email yuborilmadi (SMTP sozlanmagan): to=%s subject=%r",
                ", ".join(mask_email(address) for address in message.to),
                message.subject,
            )
        return 0
