import logging
from rest_framework import generics, permissions, status
from rest_framework.exceptions import AuthenticationFailed, Throttled, ValidationError
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView
from .avatar import AvatarError, delete_old_avatar, process_avatar
from .models import User
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
        security_logger.info(
            "Muvaffaqiyatli kirish: %r (IP: %s)",
            request.data.get("username"),
            client_ident(request),
        )
        return response


class LogoutView(APIView):
    """Refresh tokenni qora ro'yxatga qo'shib, sessiyani butunlay yopadi."""

    # Refresh tokenning o'zi credential: access eskirgan bo'lsa ham logout ishlashi kerak,
    # aks holda 401 qaytadi va blacklist chaqirilmaydi. Sarlavhadagi access token e'tiborga olinmaydi.
    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "logout"

    def post(self, request):
        try:
            token = RefreshToken(request.data["refresh"])
            token.blacklist()
        except (KeyError, TypeError, TokenError):
            return Response({"detail": "Yaroqsiz token."}, status=status.HTTP_400_BAD_REQUEST)
        security_logger.info(
            "Chiqish: user_id=%s (IP: %s)",
            token.get("user_id"),
            client_ident(request),
        )
        return Response(status=status.HTTP_205_RESET_CONTENT)


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
            user.set_password(new_password)
            user.save(update_fields=["password"])
            # Hisobni egallagan kishi parol tiklangach ham sessiyada qolmasin (7-band)
            revoked = revoke_refresh_tokens(user)

        security_logger.info("Parol muvaffaqiyatli tiklandi: %s (%s ta sessiya yopildi)", user.username, revoked)
        return Response({"detail": "Parol muvaffaqiyatli yangilandi."})