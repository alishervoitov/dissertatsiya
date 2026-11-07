import hmac

from django.conf import settings
from rest_framework.permissions import BasePermission


class HasKioskKey(BasePermission):
    """Kiosk ekrani hisobsiz ishlaydi, lekin faqat maxfiy kalit bilan:
      * administrator panelida yaratilgan kiosk kaliti (appointments/kiosks.py) - asosiy yo'l;
      * .env'dagi QUEUE_KIOSK_KEY - o'tish davri uchun (bo'sh bo'lsa o'chiq).
    Ikkalasi ham yo'q bo'lsa kiosk butunlay yopiq."""

    message = "Kiosk kaliti noto'g'ri yoki kiosk o'chirilgan."

    def has_permission(self, request, view):
        from users.security import client_ident

        from .kiosks import find_device, touch

        given = request.headers.get("X-Kiosk-Key", "")
        device = find_device(given)
        if device:
            touch(device, client_ident(request))
            request.kiosk = device
            return True
        expected = getattr(settings, "QUEUE_KIOSK_KEY", "")
        return bool(expected) and hmac.compare_digest(given.encode(), expected.encode())
