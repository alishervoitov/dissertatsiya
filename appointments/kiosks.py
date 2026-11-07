"""
Kiosk kalitlarini administrator panelidan boshqarish.

Avval bitta umumiy kalit .env.docker'dagi QUEUE_KIOSK_KEY da edi: uni almashtirish uchun serverga
kirib konteynerni qayta ishga tushirish kerak bo'lardi, kalit o'g'irlansa barcha kiosklar birga
to'xtardi va qaysi kiosk qachon ishlagani ko'rinmasdi. Endi:
  * har bir kioskka alohida kalit; bazada faqat SHA-256 xeshi;
  * kalit yaratilganda bir marta ko'rsatiladi;
  * bekor qilish darhol kuchga kiradi (server qayta ishga tushmaydi);
  * oxirgi foydalanish vaqti va IP ko'rinadi;
  * yaratish va bekor qilish auditga yoziladi, yangi kalit Telegram'ga xabar qilinadi.
Eski QUEUE_KIOSK_KEY o'tish davri uchun ishlashda davom etadi va ro'yxatda alohida ko'rsatiladi.
"""
import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from records.audit import log_audit
from users.permissions import IsAdmin

from .models import KioskDevice

KEY_PREFIX = "ks_"
TOUCH_EVERY = timedelta(minutes=5)   # har so'rovda bazaga yozmaslik uchun
MAX_ACTIVE = 50


def hash_key(key):
    return hashlib.sha256((key or "").encode()).hexdigest()


def new_key():
    return KEY_PREFIX + secrets.token_urlsafe(24)


def find_device(key):
    """Kalit bo'yicha faol kiosk yoki None."""
    if not key or not key.startswith(KEY_PREFIX) or len(key) > 100:
        return None
    return KioskDevice.objects.filter(key_hash=hash_key(key), revoked_at__isnull=True).first()


def touch(device, ip):
    now = timezone.now()
    if device.last_used_at is None or now - device.last_used_at >= TOUCH_EVERY or device.last_ip != ip:
        KioskDevice.objects.filter(pk=device.pk).update(last_used_at=now, last_ip=ip or None)


def legacy_key_active():
    return bool(getattr(settings, "QUEUE_KIOSK_KEY", ""))


def device_data(d):
    return {
        "id": d.id,
        "name": d.name,
        "key_prefix": d.key_prefix,
        "created_at": d.created_at,
        "created_by": (d.created_by.get_full_name() or d.created_by.username) if d.created_by else None,
        "last_used_at": d.last_used_at,
        "last_ip": d.last_ip,
        "revoked_at": d.revoked_at,
    }


class KioskCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=80)

    def validate_name(self, v):
        v = " ".join(v.split())
        if not v:
            raise serializers.ValidationError("Kiosk nomini kiriting.")
        return v


class KioskListView(APIView):
    """GET - kiosklar ro'yxati; POST {name} - yangi kiosk va kalit (kalit faqat shu javobda)."""

    permission_classes = [IsAdmin]

    def get(self, request):
        devices = KioskDevice.objects.select_related("created_by")
        return Response({"results": [device_data(d) for d in devices], "legacy_key_active": legacy_key_active()})

    def post(self, request):
        s = KioskCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        if KioskDevice.objects.filter(revoked_at__isnull=True).count() >= MAX_ACTIVE:
            return Response({"detail": "Faol kiosklar soni chegarasiga yetdi."}, status=status.HTTP_400_BAD_REQUEST)
        key = new_key()
        device = KioskDevice.objects.create(
            name=s.validated_data["name"], key_hash=hash_key(key), key_prefix=key[:8], created_by=request.user,
        )
        log_audit(request.user, "create", detail=f"Kiosk kaliti yaratildi: «{device.name}» ({device.key_prefix}…)")
        from monitoring.notify import esc, notify
        notify(f"🟡 <b>ShifoSafe: yangi kiosk kaliti</b>\n«{esc(device.name, 80)}» ({device.key_prefix}…)\n"
               f"Yaratdi: {esc(request.user.username, 64)}\nAgar bu siz bo'lmasangiz - darhol bekor qiling.")
        response = Response({**device_data(device), "key": key}, status=status.HTTP_201_CREATED)
        response["Cache-Control"] = "no-store"
        return response


class KioskRevokeView(APIView):
    """POST - kioskni bekor qilish: kalit darhol ishlamay qoladi."""

    permission_classes = [IsAdmin]

    def post(self, request, pk):
        device = KioskDevice.objects.filter(pk=pk).first()
        if not device:
            return Response({"detail": "Kiosk topilmadi."}, status=status.HTTP_404_NOT_FOUND)
        if device.revoked_at is None:
            device.revoked_at = timezone.now()
            device.revoked_by = request.user
            device.save(update_fields=["revoked_at", "revoked_by"])
            log_audit(request.user, "delete", detail=f"Kiosk kaliti bekor qilindi: «{device.name}» ({device.key_prefix}…)")
        return Response(device_data(device))
