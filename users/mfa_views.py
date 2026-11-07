"""2FA endpointlari: kirishning ikkinchi bosqichi, sozlash, zaxira kodlar, admin tomonidan tiklash."""
import logging

from django.contrib.auth.models import update_last_login
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from appointments.checkin import qr_svg
from records.audit import log_audit

from . import mfa, sessions
from .auth_cookies import set_refresh_cookie
from .models import TwoFactorDevice, User
from .permissions import IsAdmin
from .security import client_ident, revoke_refresh_tokens
from .serializers import CustomTokenObtainPairSerializer, UserPublicSerializer

security_logger = logging.getLogger("security")


def _bad(detail, code, http=status.HTTP_400_BAD_REQUEST):
    return Response({"detail": detail, "code": code}, status=http)


def issue_tokens(request, user):
    """Login javobi bilan bir xil: tokenlar + sessiya yozuvi."""
    refresh = CustomTokenObtainPairSerializer.get_token(user)
    try:
        sessions.start_session(request, user, refresh)
    except Exception:
        security_logger.exception("Login sessiyasini yozib bo'lmadi")
    update_last_login(None, user)
    return {
        "refresh": str(refresh),
        "access": str(refresh.access_token),
        "role": user.role,
        "user": UserPublicSerializer(user, context={"request": request}).data,
    }


class MfaLoginView(APIView):
    """POST {mfa_token, code} - parol to'g'ri kiritilgandan keyingi ikkinchi bosqich."""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "mfa"

    def post(self, request):
        parsed = mfa.read_login_token(request.data.get("mfa_token"))
        if not parsed:
            return _bad("Kirish muddati tugadi. Login va parolni qaytadan kiriting.", "mfa_expired",
                        status.HTTP_401_UNAUTHORIZED)
        user_id, nonce = parsed
        user = User.objects.filter(pk=user_id, is_active=True).first()
        method = mfa.verify_any(user, request.data.get("code")) if user else None
        if not method:
            tries = mfa.register_failed_attempt(nonce)
            security_logger.warning("2FA kodi noto'g'ri: %r (IP: %s, urinish %s)",
                                    getattr(user, "username", user_id), client_ident(request), tries)
            left = max(0, mfa.MFA_MAX_ATTEMPTS - tries)
            if not left:
                return _bad("Urinishlar tugadi. Login va parolni qaytadan kiriting.", "mfa_expired",
                            status.HTTP_401_UNAUTHORIZED)
            return Response({"detail": "Kod noto'g'ri.", "code": "mfa_invalid", "attempts_left": left},
                            status=status.HTTP_400_BAD_REQUEST)
        mfa.consume_login_token(nonce)
        data = issue_tokens(request, user)
        refresh = data.pop("refresh")
        if method == "backup":
            data["backup_remaining"] = mfa.backup_remaining(user)
            security_logger.warning("Zaxira kod bilan kirildi: %r (IP: %s, qoldi %s)",
                                    user.username, client_ident(request), data["backup_remaining"])
        security_logger.info("Muvaffaqiyatli kirish (2FA, %s): %r (IP: %s)", method, user.username, client_ident(request))
        # Refresh token - faqat httpOnly cookie'da (users/auth_cookies.py)
        return set_refresh_cookie(Response(data), refresh)


class MfaStatusView(APIView):
    def get(self, request):
        user = request.user
        device = mfa.enabled_device(user)
        return Response({
            "enabled": bool(device),
            "required": mfa.required_for(user),
            "confirmed_at": device.confirmed_at if device else None,
            "backup_remaining": mfa.backup_remaining(user) if device else 0,
        })


class MfaSetupView(APIView):
    """POST - yangi kalit (QR) yaratadi. Tasdiqlanmaguncha 2FA yoqilmaydi."""

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "mfa_manage"

    def post(self, request):
        user = request.user
        if mfa.enabled_device(user):
            return _bad("Ikki bosqichli kirish allaqachon yoqilgan.", "mfa_already_enabled", status.HTTP_409_CONFLICT)
        secret = mfa.new_secret()
        TwoFactorDevice.objects.update_or_create(user=user, defaults={"secret": secret, "confirmed_at": None, "last_step": 0})
        uri = mfa.otpauth_uri(user, secret)
        return Response({"secret": secret, "otpauth_uri": uri, "qr_svg": qr_svg(uri)})


class MfaConfirmView(APIView):
    """POST {code} - ilovadagi birinchi kod bilan tasdiqlash; zaxira kodlar bir marta ko'rsatiladi."""

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "mfa_manage"

    def post(self, request):
        user = request.user
        device = TwoFactorDevice.objects.filter(user=user, confirmed_at__isnull=True).first()
        if not device:
            return _bad("Avval QR kodni oling.", "mfa_no_setup")
        if not mfa.verify_totp(device, request.data.get("code")):
            return _bad("Kod noto'g'ri. Ilovadagi joriy 6 xonali kodni kiriting.", "mfa_invalid")
        device.confirmed_at = timezone.now()
        device.save(update_fields=["confirmed_at"])
        user.is_active_2fa = True
        user.save(update_fields=["is_active_2fa"])
        codes = mfa.new_backup_codes(user)
        log_audit(user, "update", detail="Ikki bosqichli kirish yoqildi")
        security_logger.info("2FA yoqildi: %r", user.username)
        return Response({"enabled": True, "backup_codes": codes})


class MfaBackupCodesView(APIView):
    """POST {code} - zaxira kodlarni yangilash (eskilari bekor bo'ladi)."""

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "mfa_manage"

    def post(self, request):
        user = request.user
        device = mfa.enabled_device(user)
        if not device:
            return _bad("Ikki bosqichli kirish yoqilmagan.", "mfa_not_enabled")
        if not mfa.verify_totp(device, request.data.get("code")):
            return _bad("Kod noto'g'ri. Ilovadagi joriy 6 xonali kodni kiriting.", "mfa_invalid")
        codes = mfa.new_backup_codes(user)
        log_audit(user, "update", detail="Zaxira kodlar yangilandi")
        return Response({"backup_codes": codes})


class MfaDisableView(APIView):
    """POST {code, password} - 2FA ni o'chirish (majburiy rollar uchun mumkin emas)."""

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "mfa_manage"

    def post(self, request):
        user = request.user
        if mfa.required_for(user):
            return _bad("Sizning rolingiz uchun ikki bosqichli kirish majburiy.", "mfa_required_role",
                        status.HTTP_403_FORBIDDEN)
        if not mfa.enabled_device(user):
            return _bad("Ikki bosqichli kirish yoqilmagan.", "mfa_not_enabled")
        if not user.check_password(request.data.get("password") or ""):
            return _bad("Parol noto'g'ri.", "wrong_password")
        if not mfa.verify_any(user, request.data.get("code")):
            return _bad("Kod noto'g'ri.", "mfa_invalid")
        mfa.disable(user)
        log_audit(user, "update", detail="Ikki bosqichli kirish o'chirildi")
        security_logger.warning("2FA o'chirildi: %r", user.username)
        return Response({"enabled": False})


class AdminMfaResetView(APIView):
    """POST - xodim telefonini ham, zaxira kodlarini ham yo'qotganda administrator 2FA ni tiklaydi.
    Barcha sessiyalari yopiladi; keyingi kirishda u qaytadan sozlaydi."""

    permission_classes = [IsAdmin]

    def post(self, request, pk):
        target = User.objects.filter(pk=pk).first()
        if not target:
            return _bad("Foydalanuvchi topilmadi.", "not_found", status.HTTP_404_NOT_FOUND)
        if target.pk == request.user.pk:
            return _bad("O'z 2FA ingizni tiklay olmaysiz - buni boshqa administrator qiladi.", "self_reset",
                        status.HTTP_403_FORBIDDEN)
        if not target.is_active_2fa and not TwoFactorDevice.objects.filter(user=target).exists():
            return _bad("Bu foydalanuvchida 2FA yoqilmagan.", "mfa_not_enabled")
        mfa.disable(target)
        revoke_refresh_tokens(target)
        sessions.revoke(sessions.active_sessions(target))
        log_audit(request.user, "update", detail=f"2FA tiklandi: {target.username}")
        security_logger.warning("Administrator %r foydalanuvchi %r 2FA sini tikladi", request.user.username, target.username)
        return Response({"reset": True})
