import hmac

from django.conf import settings
from rest_framework.permissions import BasePermission


class HasKioskKey(BasePermission):
    """Kiosk ekrani hisobsiz ishlaydi, lekin faqat sozlamadagi maxfiy kalit bilan.
    Kalit sozlanmagan bo'lsa kiosk butunlay o'chiq."""

    message = "Kiosk kaliti noto'g'ri yoki kiosk o'chirilgan."

    def has_permission(self, request, view):
        expected = getattr(settings, "QUEUE_KIOSK_KEY", "")
        given = request.headers.get("X-Kiosk-Key", "")
        return bool(expected) and hmac.compare_digest(given.encode(), expected.encode())
