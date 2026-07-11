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
    """Joriy tizimga kirgan foydalanuvchi haqida ma'lumot."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        return Response(UserPublicSerializer(request.user).data)


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