from django.shortcuts import render
import logging
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView
from .models import User
from .permissions import IsAdmin
from .serializers import (
    AdminCreateUserSerializer,
    CustomTokenObtainPairSerializer,
    RegisterSerializer,
    UserPublicSerializer,
)
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.conf import settings
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
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
        response = super().create(request, *args, **kwargs)
        security_logger.info(
            "Yangi bemor ro'yxatdan o'tdi: %s (IP: %s)",
            request.data.get("username"),
            request.META.get("REMOTE_ADDR"),
        )
        return response


class LoginView(TokenObtainPairView):
    """Login - muvaffaqiyatsiz urinishlar 5/daqiqa bilan cheklangan (brute-force himoyasi)."""

    serializer_class = CustomTokenObtainPairSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"

    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        if response.status_code == 200:
            security_logger.info(
                "Muvaffaqiyatli kirish: %s (IP: %s)",
                request.data.get("username"),
                request.META.get("REMOTE_ADDR"),
            )
        else:
            security_logger.warning(
                "Muvaffaqiyatsiz kirish urinishi: %s (IP: %s)",
                request.data.get("username"),
                request.META.get("REMOTE_ADDR"),
            )
        return response


class LogoutView(APIView):
    """Refresh tokenni qora ro'yxatga qo'shib, sessiyani butunlay yopadi."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        try:
            refresh_token = request.data["refresh"]
            token = RefreshToken(refresh_token)
            token.blacklist()
        except Exception:
            return Response({"detail": "Yaroqsiz token."}, status=status.HTTP_400_BAD_REQUEST)
        security_logger.info("Chiqish: %s", request.user.username)
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

    queryset = User.objects.all().order_by("-created_at")
    permission_classes = [IsAdmin]

    def get_serializer_class(self):
        if self.request.method == "POST":
            return AdminCreateUserSerializer
        return UserPublicSerializer

    def perform_create(self, serializer):
        user = serializer.save()
        security_logger.info(
            "Administrator %s tomonidan yangi hisob yaratildi: %s (rol: %s)",
            self.request.user.username, user.username, user.role,
        )


class AdminUserDetailView(generics.RetrieveUpdateDestroyAPIView):
    """Administrator uchun bitta foydalanuvchini boshqarish (faollashtirish/o'chirish)."""

    queryset = User.objects.all()
    serializer_class = UserPublicSerializer
    permission_classes = [IsAdmin]

from rest_framework.parsers import MultiPartParser, FormParser


class AvatarUploadView(APIView):
    """Joriy foydalanuvchi o'z avatarini yuklaydi/yangilaydi."""

    permission_classes = [permissions.IsAuthenticated]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        avatar = request.FILES.get("avatar")
        if not avatar:
            return Response({"detail": "Rasm fayli topilmadi."}, status=status.HTTP_400_BAD_REQUEST)

        if avatar.size > 5 * 1024 * 1024:  # 5 MB chegarasi
            return Response({"detail": "Fayl hajmi 5 MB dan oshmasligi kerak."}, status=status.HTTP_400_BAD_REQUEST)

        if not avatar.content_type.startswith("image/"):
            return Response({"detail": "Faqat rasm fayllari qabul qilinadi."}, status=status.HTTP_400_BAD_REQUEST)

        request.user.avatar = avatar
        request.user.save()
        return Response(UserPublicSerializer(request.user, context={"request": request}).data)





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

        user = User.objects.filter(email__iexact=email).first()
        if not user:
            security_logger.info("Parolni tiklash so'ralgan, lekin email topilmadi: %s", email)
            return generic_response

        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        reset_link = f"{settings.FRONTEND_URL}/reset-password/{uid}/{token}/"

        send_mail(
            subject="MedKarta — Parolni tiklash",
            message=(
                f"Salom, {user.get_full_name() or user.username}!\n\n"
                f"Parolingizni tiklash uchun quyidagi havolaga o'ting:\n{reset_link}\n\n"
                f"Agar buni siz so'ramagan bo'lsangiz, bu xabarni e'tiborsiz qoldiring."
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
        )
        security_logger.info("Parolni tiklash havolasi yuborildi: %s", user.username)
        return generic_response


class PasswordResetConfirmView(APIView):
    """Havoladagi uid/token asosida yangi parolni o'rnatadi."""

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        uid = request.data.get("uid")
        token = request.data.get("token")
        new_password = request.data.get("new_password")

        if not all([uid, token, new_password]):
            return Response({"detail": "Barcha maydonlar to'ldirilishi shart."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            user_id = force_str(urlsafe_base64_decode(uid))
            user = User.objects.get(pk=user_id)
        except (User.DoesNotExist, ValueError, TypeError, OverflowError):
            return Response({"detail": "Havola yaroqsiz."}, status=status.HTTP_400_BAD_REQUEST)

        if not default_token_generator.check_token(user, token):
            return Response({"detail": "Havola yaroqsiz yoki muddati tugagan."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            validate_password(new_password, user=user)
        except DjangoValidationError as e:
            return Response({"detail": list(e.messages)}, status=status.HTTP_400_BAD_REQUEST)

        user.set_password(new_password)
        user.failed_login_attempts = 0
        user.locked_until = None
        user.save()

        security_logger.info("Parol muvaffaqiyatli tiklandi: %s", user.username)
        return Response({"detail": "Parol muvaffaqiyatli yangilandi."})