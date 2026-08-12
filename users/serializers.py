from django.contrib.auth import password_validation
from django.db import transaction
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework import serializers
from .models import User
from datetime import timedelta
from django.utils import timezone
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from .models import Role, User


class UserPublicSerializer(serializers.ModelSerializer):
    """Parolsiz, faqat ochiq profil ma'lumotlari."""

    class Meta:
        model = User
        fields = ["id", "username", "first_name", "last_name", "email", "phone", "role", "created_at"]
        read_only_fields = ["id", "role", "created_at"]


class RegisterSerializer(serializers.ModelSerializer):
    """
    Ommaviy ro'yxatdan o'tish faqat 'bemor' roli uchun ochiq.
    Shifokor va administrator hisoblari faqat administrator tomonidan yaratiladi
    (privilege escalation oldini olish uchun).
    """

    password = serializers.CharField(write_only=True, min_length=10)
    password_confirm = serializers.CharField(write_only=True, min_length=10)

    class Meta:
        model = User
        fields = [
            "id", "username", "first_name", "last_name",
            "email", "phone", "password", "password_confirm",
        ]

    def validate(self, attrs):
        if attrs["password"] != attrs.pop("password_confirm"):
            raise serializers.ValidationError({"password_confirm": "Parollar mos kelmadi."})
        password_validation.validate_password(attrs["password"])
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        password = validated_data.pop("password")
        user = User(role=Role.PATIENT, **validated_data)
        user.set_password(password)  # Argon2 hash - hech qachon ochiq matn saqlanmaydi
        user.save()
        return user


class AdminCreateUserSerializer(serializers.ModelSerializer):
    """Administrator shifokor yoki boshqa admin hisoblarini yaratishi uchun."""

    password = serializers.CharField(write_only=True, min_length=10)

    class Meta:
        model = User
        fields = [
            "id", "username", "first_name", "last_name",
            "email", "phone", "password", "role",
        ]

    def validate_password(self, value):
        password_validation.validate_password(value)
        return value

    def create(self, validated_data):
        password = validated_data.pop("password")
        user = User(**validated_data)
        user.set_password(password)
        user.save()
        return user




MAX_FAILED_ATTEMPTS = 5
LOCKOUT_DURATION = timedelta(minutes=15)


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["role"] = user.role
        token["full_name"] = user.get_full_name() or user.username
        return token

    def validate(self, attrs):
        username = attrs.get("username")
        user = User.objects.filter(username=username).first()

        if user and user.is_locked:
            remaining = int((user.locked_until - timezone.now()).total_seconds() / 60) + 1
            raise serializers.ValidationError(
                f"Hisob vaqtincha bloklangan. {remaining} daqiqadan so'ng qayta urinib ko'ring."
            )

        try:
            data = super().validate(attrs)
        except Exception:
            # Muvaffaqiyatsiz urinish - hisoblagichni oshiramiz
            if user:
                user.failed_login_attempts += 1
                if user.failed_login_attempts >= MAX_FAILED_ATTEMPTS:
                    user.locked_until = timezone.now() + LOCKOUT_DURATION
                    user.failed_login_attempts = 0
                user.save(update_fields=["failed_login_attempts", "locked_until"])
            raise

        # Muvaffaqiyatli login - hisoblagichni tozalaymiz
        if user:
            user.failed_login_attempts = 0
            user.locked_until = None
            user.save(update_fields=["failed_login_attempts", "locked_until"])

        data["role"] = self.user.role
        data["user"] = UserPublicSerializer(self.user, context=self.context).data
        return data

class UserPublicSerializer(serializers.ModelSerializer):
    """Parolsiz, faqat ochiq profil ma'lumotlari."""

    avatar_url = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ["id", "username", "first_name", "last_name", "email", "phone", "role", "avatar_url", "created_at"]
        read_only_fields = ["id", "role", "created_at"]

    def get_avatar_url(self, obj):
        request = self.context.get("request")
        if obj.avatar and request:
            return request.build_absolute_uri(obj.avatar.url)
        return None