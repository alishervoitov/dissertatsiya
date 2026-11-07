import logging
from rest_framework import generics, permissions, status
from rest_framework.exceptions import AuthenticationFailed, Throttled, ValidationError
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView
from .avatar import AvatarError, delete_old_avatar, process_avatar
from . import sessions
from .auth_cookies import clear_refresh_cookie, csrf_header_ok, move_refresh_to_cookie, read_refresh, set_refresh_cookie
from .models import LoginSession, User
from .permissions import IsAdmin
from .security import client_ident, record_register_taken, register_taken_exceeded, revoke_refresh_tokens
from .tasks import request_password_reset
from .serializers import (
    AdminCreateUserSerializer,
    AdminUserListSerializer,
    CustomTokenObtainPairSerializer,
    RegisterSerializer,
    UserPublicSerializer,
)
from django.contrib.auth.tokens import default_token_generator
from django.db import transaction
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
security_logger = logging.getLogger("security")


class RegisterView(generics.CreateAPIView):
    """Ommaviy ro'yxatdan o'tish - har doim 'bemor' roli bilan yaratiladi."""

    queryset = User.objects.all()
    serializer_class = RegisterSerializer
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "register"

    def create(self, request, *args, **kwargs):
        # "Username band" javobi hisob borligini bildiradi: bunday javoblar IP bo'yicha
        # alohida cheklanadi va logga yoziladi (pentest 2026-10-19, 5-band).
        ident = client_ident(request)
        if register_taken_exceeded(ident):
            security_logger.warning("Register: band username cheklovi oshdi (IP: %s)", ident)
            raise Throttled(detail="Juda ko'p urinish. Birozdan so'ng qayta urinib ko'ring.")

        serializer = self.get_serializer(data=request.data)
        if not serializer.is_valid():
            if any(error.code == "unique" for error in serializer.errors.get("username", [])):
                record_register_taken(ident)
                security_logger.warning("Register: band username so'raldi (IP: %s)", ident)
            raise ValidationError(serializer.errors)

        self.perform_create(serializer)
        response = Response(serializer.data, status=status.HTTP_201_CREATED, headers=self.get_success_headers(serializer.data))
        security_logger.info(
            "Yangi bemor ro'yxatdan o'tdi: %s (IP: %s)",
            request.data.get("username"),
            client_ident(request),
        )
        return response


class LoginView(TokenObtainPairView):
    """Login - muvaffaqiyatsiz urinishlar 5/daqiqa bilan cheklangan (brute-force himoyasi)."""

    serializer_class = CustomTokenObtainPairSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"

    def post(self, request, *args, **kwargs):
        # Xato login exception bilan tugaydi, shuning uchun uni shu yerda ushlab yozamiz (log bandi).
        # Username foydalanuvchi kiritgan qiymat: %r - yangi qator bilan soxta log yozuvi yasab bo'lmasin.
        try:
            response = super().post(request, *args, **kwargs)
        except AuthenticationFailed:
            security_logger.warning(
                "Muvaffaqiyatsiz kirish urinishi: %r (IP: %s)",
                request.data.get("username"),
                client_ident(request),
            )
            raise
        if response.data.get("password_change_required"):
            security_logger.info("Vaqtinchalik parol bilan kirish, yangi parol kutilmoqda: %r (IP: %s)",
                                 request.data.get("username"), client_ident(request))
            return response
        if response.data.get("mfa_required"):
            security_logger.info("Parol to'g'ri, 2FA kodi kutilmoqda: %r (IP: %s)",
                                 request.data.get("username"), client_ident(request))
            return response
        try:
            refresh = RefreshToken(response.data["refresh"])
            sessions.start_session(request, User.objects.get(pk=refresh["user_id"]), refresh)
        except Exception:  # sessiya yozuvi kirishni to'xtatmasin
            security_logger.exception("Login sessiyasini yozib bo'lmadi")
        security_logger.info(
            "Muvaffaqiyatli kirish: %r (IP: %s)",
            request.data.get("username"),
            client_ident(request),
        )
        # Refresh token javob tanasida emas, httpOnly cookie'da (users/auth_cookies.py)
        return move_refresh_to_cookie(response)


class LogoutView(APIView):
    """Cookie'dagi refresh tokenni qora ro'yxatga qo'shib, sessiyani yopadi va cookie'ni o'chiradi."""

    # Refresh tokenning o'zi credential: access eskirgan bo'lsa ham logout ishlashi kerak,
    # aks holda 401 qaytadi va blacklist chaqirilmaydi. Sarlavhadagi access token e'tiborga olinmaydi.
    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "logout"

    def post(self, request):
        if not csrf_header_ok(request):
            return Response({"detail": "So'rov rad etildi.", "code": "csrf"}, status=status.HTTP_403_FORBIDDEN)
        raw = read_refresh(request)
        user_id = None
        if raw:
            try:
                token = RefreshToken(raw)
                token.blacklist()
                sessions.mark_revoked_jtis([token["jti"]])
                user_id = token.get("user_id")
            except TokenError:
                pass                         # muddati o'tgan yoki allaqachon yopilgan - baribir chiqamiz
        security_logger.info("Chiqish: user_id=%s (IP: %s)", user_id, client_ident(request))
        return clear_refresh_cookie(Response(status=status.HTTP_205_RESET_CONTENT))


class MeView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response(UserPublicSerializer(request.user, context={"request": request}).data)


class AdminUserListCreateView(generics.ListCreateAPIView):
    """
    Administrator panel: barcha foydalanuvchilarni ko'rish va
    yangi shifokor/administrator hisoblarini yaratish.
    """

    queryset = User.objects.select_related("doctor_profile").order_by("-created_at")
    permission_classes = [IsAdmin]

    def get_serializer_class(self):
        if self.request.method == "POST":
            return AdminCreateUserSerializer
        return AdminUserListSerializer

    def perform_create(self, serializer):
        user = serializer.save()
        security_logger.info(
            "Administrator %s tomonidan yangi hisob yaratildi: %s (rol: %s)",
            self.request.user.username, user.username, user.role,
        )


class AdminUserDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Administrator uchun bitta foydalanuvchini boshqarish (faollashtirish/o'chirish)."""

    queryset = User.objects.select_related("doctor_profile")
    serializer_class = AdminUserListSerializer
    permission_classes = [IsAdmin]

    def perform_update(self, serializer):
        before = serializer.instance.is_active
        user = serializer.save()
        if before != user.is_active:
            security_logger.info(
                "Administrator %s hisobni %s: %s",
                self.request.user.username, "faollashtirdi" if user.is_active else "faolsizlantirdi", user.username,
            )

from rest_framework.parsers import MultiPartParser, FormParser


class AvatarUploadView(APIView):
    """Joriy foydalanuvchi o'z avatarini yuklaydi/yangilaydi."""

    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "avatar"

    def post(self, request):
        avatar = request.FILES.get("avatar")
        if not avatar:
            return Response({"detail": "Rasm fayli topilmadi."}, status=status.HTTP_400_BAD_REQUEST)

        # Content-Type mijoz tomonidan beriladi, unga ishonilmaydi: fayl rasm sifatida ochilib qayta kodlanadi.
        try:
            content, name = process_avatar(avatar)
        except AvatarError as exc:
            security_logger.warning(
                "Avatar rad etildi: %s (fayl: %r, %s bayt, IP: %s): %s",
                request.user.username,
                avatar.name,
                avatar.size,
                client_ident(request),
                exc,
            )
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        user = request.user
        old_name = user.avatar.name if user.avatar else ""
        storage = user.avatar.storage
        with transaction.atomic():
            user.avatar.save(name, content, save=False)
            user.save(update_fields=["avatar"])
            # Eski fayl faqat yangisi bazaga yozilgandan keyin o'chiriladi
            transaction.on_commit(lambda: delete_old_avatar(storage, old_name))
        return Response(UserPublicSerializer(user, context={"request": request}).data)





class PasswordResetRequestView(APIView):
    """
    Email manzil orqali parolni tiklash havolasini yuboradi.
    Xavfsizlik uchun: email tizimda bormi-yo'qmi - bir xil javob qaytariladi
    (bu orqali kimning ro'yxatdan o'tganini "sinab ko'rish" imkoniyati yo'qoladi).
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "password_reset"

    def post(self, request):
        email = request.data.get("email", "").strip()
        generic_response = Response(
            {"detail": "Agar shu email bilan hisob mavjud bo'lsa, tiklash havolasi yuborildi."}
        )

        if not email:
            return Response({"detail": "Email kiritilishi shart."}, status=status.HTTP_400_BAD_REQUEST)

        # Hisobni qidirish fon vazifasida: javob vaqti email mavjudligiga bog'liq bo'lmasin (5-band).
        # Broker (Redis) ishlamasa ham javob bir xil: 500 xatosi foydalanuvchiga ko'rsatilmaydi, logga yoziladi.
        try:
            request_password_reset.delay(email)
        except Exception:
            security_logger.error("Parolni tiklash vazifasini navbatga qo'yib bo'lmadi", exc_info=True)
        return generic_response


class PasswordResetConfirmView(APIView):
    """Havoladagi uid/token asosida yangi parolni o'rnatadi."""

    permission_classes = [permissions.AllowAny]
    # Token taxmin qilib bo'lmaydi (HMAC), lekin cheksiz urinishlar shovqin va resurs sarfi (7-band)
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "password_reset_confirm"

    def post(self, request):
        uid = request.data.get("uid")
        token = request.data.get("token")
        new_password = request.data.get("new_password")

        if not all([uid, token, new_password]):
            return Response({"detail": "Barcha maydonlar to'ldirilishi shart."}, status=status.HTTP_400_BAD_REQUEST)

        # Mavjud bo'lmagan uid va noto'g'ri token uchun bir xil javob - foydalanuvchi ID'lari oshkor bo'lmasin (5-band).
        invalid_link = Response({"detail": "Havola yaroqsiz yoki muddati tugagan."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            user_id = force_str(urlsafe_base64_decode(uid))
            user = User.objects.get(pk=user_id)
        except (User.DoesNotExist, ValueError, TypeError, OverflowError):
            return invalid_link

        if not default_token_generator.check_token(user, token):
            return invalid_link

        try:
            validate_password(new_password, user=user)
        except DjangoValidationError as e:
            return Response({"detail": list(e.messages)}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            from . import temp_password
            user.set_password(new_password)
            temp_password.clear(user)
            user.save(update_fields=["password", "must_change_password", "temp_password_expires"])
            # Hisobni egallagan kishi parol tiklangach ham sessiyada qolmasin (7-band)
            revoked = revoke_refresh_tokens(user)

        security_logger.info("Parol muvaffaqiyatli tiklandi: %s (%s ta sessiya yopildi)", user.username, revoked)
        return Response({"detail": "Parol muvaffaqiyatli yangilandi."})

class SessionRefreshView(TokenRefreshView):
    """httpOnly cookie'dagi refresh token bilan yangi access token beradi va sessiya faolligini belgilaydi.
    Refresh token tanada qabul qilinmaydi va qaytarilmaydi - JavaScript uni hech qachon ko'rmaydi."""

    def post(self, request, *args, **kwargs):
        if not csrf_header_ok(request):
            return Response({"detail": "So'rov rad etildi.", "code": "csrf"}, status=status.HTTP_403_FORBIDDEN)
        raw = read_refresh(request)
        if not raw:
            return Response({"detail": "Sessiya topilmadi. Qaytadan kiring.", "code": "no_session"},
                            status=status.HTTP_401_UNAUTHORIZED)
        serializer = self.get_serializer(data={"refresh": raw})
        try:
            serializer.is_valid(raise_exception=True)
        except (TokenError, InvalidToken):
            # Yopilgan, muddati o'tgan yoki soxta cookie - uni ham o'chiramiz
            return clear_refresh_cookie(Response({"detail": "Sessiya tugagan. Qaytadan kiring.",
                                                  "code": "token_not_valid"}, status=status.HTTP_401_UNAUTHORIZED))
        try:
            sessions.touch_session(request, RefreshToken(raw))
        except Exception:
            security_logger.exception("Sessiya faolligini yangilab bo'lmadi")
        data = dict(serializer.validated_data)
        response = Response({"access": data["access"]})
        if data.get("refresh"):                  # ROTATE_REFRESH_TOKENS yoqilsa - yangisi cookie'ga
            set_refresh_cookie(response, data["refresh"])
        return response


def _session_data(s, current_sid):
    return {
        "id": s.id,
        "device": sessions.describe_device(s.user_agent),
        "ip": s.ip,
        "created_at": s.created_at,
        "last_seen": s.last_seen,
        "current": s.jti == current_sid,
    }


class SessionListView(APIView):
    """Foydalanuvchining faol sessiyalari (qurilmalari). Joriy qurilma belgilanadi."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        sid = request.auth.get(sessions.SID_CLAIM) if request.auth else None
        rows = [_session_data(s, sid) for s in sessions.active_sessions(request.user)]
        rows.sort(key=lambda r: not r["current"])     # joriy qurilma birinchi
        return Response({"results": rows, "current_known": bool(sid)})


class SessionRevokeView(APIView):
    """Bitta sessiyani yopish (boshqa qurilmadan chiqarish)."""

    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "sessions"

    def post(self, request, pk):
        session = sessions.active_sessions(request.user).filter(pk=pk).first()
        if not session:
            return Response({"detail": "Sessiya topilmadi."}, status=status.HTTP_404_NOT_FOUND)
        sessions.revoke([session])
        security_logger.info("Sessiya yopildi: user_id=%s session=%s (IP: %s)",
                             request.user.pk, session.pk, client_ident(request))
        return Response({"revoked": 1})


class SessionRevokeOthersView(APIView):
    """Joriy qurilmadan tashqari barcha qurilmalardan chiqish.
    include_current=true bo'lsa joriy sessiya ham yopiladi."""

    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "sessions"

    def post(self, request):
        sid = request.auth.get(sessions.SID_CLAIM) if request.auth else None
        qs = sessions.active_sessions(request.user)
        if not request.data.get("include_current"):
            qs = qs.exclude(jti=sid or "")
        count = sessions.revoke(qs)
        if request.data.get("include_current"):
            # Joylashtirishdan oldingi (sessiyasiz) tokenlar ham yopilsin
            count += revoke_refresh_tokens(request.user)
        security_logger.info("Boshqa qurilmalardan chiqildi: user_id=%s, %s ta sessiya (IP: %s)",
                             request.user.pk, count, client_ident(request))
        return Response({"revoked": count})


class TempPasswordChangeView(APIView):
    """POST {pw_token, new_password} - vaqtinchalik parol bilan kirgan bemor doimiy parol o'rnatadi.
    Shundan keyingina sessiya ochiladi (2FA yoqilgan bo'lsa - 2FA bosqichi)."""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "mfa"

    def post(self, request):
        from . import mfa, temp_password
        from .mfa_views import issue_tokens

        parsed = temp_password.read_change_token(request.data.get("pw_token"))
        if not parsed:
            return Response({"detail": "Kirish muddati tugadi. Login va vaqtinchalik parolni qaytadan kiriting.",
                             "code": "pw_token_expired"}, status=status.HTTP_401_UNAUTHORIZED)
        user, nonce = parsed
        new_password = request.data.get("new_password") or ""
        if user.check_password(new_password):
            from django.utils.translation import gettext as _
            return Response({"detail": [_("The new password must differ from the temporary one.")]},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            validate_password(new_password, user=user)
        except DjangoValidationError as e:
            return Response({"detail": list(e.messages)}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            user.set_password(new_password)
            temp_password.clear(user)
            user.save(update_fields=["password", "must_change_password", "temp_password_expires"])
            revoke_refresh_tokens(user)
        temp_password.consume_change_token(nonce)
        security_logger.info("Vaqtinchalik parol doimiysiga almashtirildi: %r (IP: %s)", user.username, client_ident(request))

        if mfa.enabled_device(user):
            return Response({"mfa_required": True, "mfa_token": mfa.issue_login_token(user)})
        data = issue_tokens(request, user)
        refresh = data.pop("refresh")
        return set_refresh_cookie(Response(data), refresh)
