from django.contrib.auth import password_validation
from django.contrib.auth.validators import UnicodeUsernameValidator
from django.db import transaction
from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.validators import UniqueValidator
from django.contrib.auth.models import update_last_login
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer, TokenObtainSerializer

from .models import Role, User
from .security import (
    LOCKOUT_DURATION,
    client_ident,
    clear_login_failures,
    is_login_locked,
    is_reserved_username,
    register_login_failure,
    security_logger,
)

# Username'ni qaytarmaydi; view "unique" kodi bo'yicha bunday javoblarni sanaydi (5-band).
USERNAME_TAKEN_MESSAGE = "Bu foydalanuvchi nomi band, boshqasini tanlang."
USERNAME_RESERVED_MESSAGE = "Bu foydalanuvchi nomidan foydalanib bo'lmaydi."
# Noto'g'ri parol, mavjud bo'lmagan hisob va bloklangan hisob uchun bir xil javob (5-band).
LOGIN_FAILED_MESSAGE = "Login yoki parol noto'g'ri, yoki hisob vaqtincha bloklangan."


CASE_INSENSITIVE_USERNAME_VALIDATORS = [
    UnicodeUsernameValidator(),
    UniqueValidator(queryset=User.objects.all(), message=USERNAME_TAKEN_MESSAGE, lookup="iexact"),
]


class ReservedOrUniqueUsernameValidator(UniqueValidator):
    """
    Avval zaxira nom (6-band), keyin bandlik tekshiriladi. Bitta validator bo'lgani uchun
    zaxira nom (masalan mavjud 'admin') "band" xatosini qo'shimcha qaytarmaydi va 5-banddagi
    "band" hisoblagichiga kirmaydi.
    """

    def __call__(self, value, serializer_field):
        if is_reserved_username(value):
            raise serializers.ValidationError(USERNAME_RESERVED_MESSAGE, code="reserved")
        super().__call__(value, serializer_field)


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
        extra_kwargs = {
            "username": {
                "validators": [
                    UnicodeUsernameValidator(),
                    ReservedOrUniqueUsernameValidator(
                        queryset=User.objects.all(), message=USERNAME_TAKEN_MESSAGE, lookup="iexact"
                    ),
                ],
            },
        }

    def validate(self, attrs):
        if attrs["password"] != attrs.pop("password_confirm"):
            raise serializers.ValidationError({"password_confirm": "Parollar mos kelmadi."})
        # Saqlanmagan User nusxasi: UserAttributeSimilarityValidator parolni
        # username/email/ism bilan solishtira olishi uchun (DB'ga tegmaydi).
        probe = User(
            username=attrs.get("username", ""),
            first_name=attrs.get("first_name", ""),
            last_name=attrs.get("last_name", ""),
            email=attrs.get("email", ""),
        )
        password_validation.validate_password(attrs["password"], probe)
        return attrs

    @transaction.atomic
    def create(self, validated_data):
        password = validated_data.pop("password")
        user = User(role=Role.PATIENT, **validated_data)
        user.set_password(password)  # PBKDF2 hash (Django sukut) - hech qachon ochiq matn saqlanmaydi
        user.save()
        return user


class AdminCreateUserSerializer(serializers.ModelSerializer):
    """Administrator shifokor yoki boshqa admin hisoblarini yaratishi uchun."""

    password = serializers.CharField(write_only=True, min_length=10)
    # Shifokor yaratilganda darhol qabul sozlamalari (navbatda shu yo'nalish bo'yicha topiladi)
    specialty = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=30)
    room = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=20)

    class Meta:
        model = User
        fields = [
            "id", "username", "first_name", "last_name",
            "email", "phone", "password", "role", "specialty", "room",
        ]
        # Zaxira nom tekshiruvi yo'q (6-band: admin xizmat hisoblarini yarata oladi), lekin registrsiz bandlik bor
        extra_kwargs = {"username": {"validators": CASE_INSENSITIVE_USERNAME_VALIDATORS}}

    def validate_password(self, value):
        data = self.initial_data
        # Saqlanmagan User nusxasi: UserAttributeSimilarityValidator uchun (DB'ga tegmaydi).
        probe = User(
            username=data.get("username", ""),
            first_name=data.get("first_name", ""),
            last_name=data.get("last_name", ""),
            email=data.get("email", ""),
        )
        password_validation.validate_password(value, probe)
        return value

    def validate_specialty(self, value):
        from appointments.models import Specialty

        if value and value not in Specialty.values:
            raise serializers.ValidationError("Noma'lum mutaxassislik.")
        return value

    def create(self, validated_data):
        password = validated_data.pop("password")
        specialty = validated_data.pop("specialty", "")
        room = validated_data.pop("room", "")
        user = User(**validated_data)
        user.set_password(password)
        user.save()
        if user.role == Role.DOCTOR:
            from appointments.models import DoctorProfile

            DoctorProfile.objects.update_or_create(user=user, defaults={"specialty": specialty, "room": room})
        return user




class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["role"] = user.role
        token["full_name"] = user.get_full_name() or user.username
        # Sessiya identifikatori: access tokenlarga ham ko'chadi, shu bilan joriy qurilma aniqlanadi
        token["sid"] = token["jti"]
        return token

    def validate(self, attrs):
        # Bloklash username+IP bo'yicha cache'da; hisob bor-yo'qligidan qat'i nazar bir xil ishlaydi.
        username = attrs.get(self.username_field)
        ident = client_ident(self.context["request"])

        if is_login_locked(username, ident):
            raise AuthenticationFailed(LOGIN_FAILED_MESSAGE, code="login_failed")

        try:
            # Faqat login/parol tekshiruvi; tokenlar 2FA kerak emasligi aniq bo'lgandan keyin yaratiladi
            data = TokenObtainSerializer.validate(self, attrs)
        except AuthenticationFailed:
            if register_login_failure(username, ident):
                minutes = int(LOCKOUT_DURATION.total_seconds() // 60)
                security_logger.warning("Login bloklandi: %r (IP: %s, %s daqiqa)", username, ident, minutes)
                from monitoring.notify import login_locked
                login_locked(username, ident, minutes)
            raise AuthenticationFailed(LOGIN_FAILED_MESSAGE, code="login_failed")

        clear_login_failures(username, ident)

        from . import mfa
        if mfa.enabled_device(self.user):
            # Parol to'g'ri, lekin tokenlar hali berilmaydi: ilovadagi kod yoki zaxira kod kerak
            return {"mfa_required": True, "mfa_token": mfa.issue_login_token(self.user)}

        refresh = self.get_token(self.user)
        data["refresh"] = str(refresh)
        data["access"] = str(refresh.access_token)
        update_last_login(None, self.user)
        data["role"] = self.user.role
        data["user"] = UserPublicSerializer(self.user, context=self.context).data
        return data

class UserPublicSerializer(serializers.ModelSerializer):
    """Parolsiz, faqat ochiq profil ma'lumotlari."""

    avatar_url = serializers.SerializerMethodField()
    mfa_enabled = serializers.BooleanField(source="is_active_2fa", read_only=True)
    mfa_required = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ["id", "username", "first_name", "last_name", "email", "phone", "role", "avatar_url", "created_at",
                  "mfa_enabled", "mfa_required"]
        read_only_fields = ["id", "role", "created_at"]
        # Admin PATCH orqali boshqa hisobning registr varianti nomiga o'tkazib bo'lmasin
        extra_kwargs = {"username": {"validators": CASE_INSENSITIVE_USERNAME_VALIDATORS}}

    def get_avatar_url(self, obj):
        request = self.context.get("request")
        if obj.avatar and request:
            return request.build_absolute_uri(obj.avatar.url)
        return None

    def get_mfa_required(self, obj):
        from .mfa import required_for
        return required_for(obj)


class AdminUserListSerializer(UserPublicSerializer):
    """Admin ro'yxati: shifokorlar uchun mutaxassislik va xona ham ko'rinadi."""

    specialty = serializers.SerializerMethodField()
    room = serializers.SerializerMethodField()

    class Meta(UserPublicSerializer.Meta):
        fields = UserPublicSerializer.Meta.fields + ["is_active", "specialty", "room"]

    def validate_is_active(self, value):
        # Administrator o'z hisobini o'chirib qo'yib, tizimdan qulflanib qolmasin
        request = self.context.get("request")
        if not value and request and self.instance and self.instance.pk == request.user.pk:
            raise serializers.ValidationError("O'z hisobingizni faolsizlantirib bo'lmaydi.")
        return value

    def _profile(self, obj):
        return getattr(obj, "doctor_profile", None) if obj.role == Role.DOCTOR else None

    def get_specialty(self, obj):
        p = self._profile(obj)
        return p.specialty if p else None

    def get_room(self, obj):
        p = self._profile(obj)
        return p.room if p else None
