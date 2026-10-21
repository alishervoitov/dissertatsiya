from django.contrib.auth import password_validation
from django.db import transaction
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework import serializers
from .models import User
from django.contrib.auth.validators import UnicodeUsernameValidator
from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.validators import UniqueValidator
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from .models import Role, User
from .security import (
    client_ident,
    clear_login_failures,
    is_login_locked,
    is_reserved_username,
    register_login_failure,
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


class UserPublicSerializer(serializers.ModelSerializer):
    """Parolsiz, faqat ochiq profil ma'lumotlari."""

    class Meta:
        model = User
        fields = ["id", "username", "first_name", "last_name", "email", "phone", "role", "created_at"]
        read_only_fields = ["id", "role", "created_at"]


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
        # Zaxira nom tekshiruvi yo'q (6-band: admin xizmat hisoblarini yarata oladi), lekin registrsiz bandlik bor
        extra_kwargs = {"username": {"validators": CASE_INSENSITIVE_USERNAME_VALIDATORS}}

    def validate_password(self, value):
        password_validation.validate_password(value)
        return value

    def create(self, validated_data):
        password = validated_data.pop("password")
        user = User(**validated_data)
        user.set_password(password)
        user.save()
        return user




class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["role"] = user.role
        token["full_name"] = user.get_full_name() or user.username
        return token

    def validate(self, attrs):
        # Bloklash username+IP bo'yicha cache'da; hisob bor-yo'qligidan qat'i nazar bir xil ishlaydi.
        username = attrs.get(self.username_field)
        ident = client_ident(self.context["request"])

        if is_login_locked(username, ident):
            raise AuthenticationFailed(LOGIN_FAILED_MESSAGE, code="login_failed")

        try:
            data = super().validate(attrs)
        except AuthenticationFailed:
            register_login_failure(username, ident)
            raise AuthenticationFailed(LOGIN_FAILED_MESSAGE, code="login_failed")

        clear_login_failures(username, ident)

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
        # Admin PATCH orqali boshqa hisobning registr varianti nomiga o'tkazib bo'lmasin
        extra_kwargs = {"username": {"validators": CASE_INSENSITIVE_USERNAME_VALIDATORS}}

    def get_avatar_url(self, obj):
        request = self.context.get("request")
        if obj.avatar and request:
            return request.build_absolute_uri(obj.avatar.url)
        return None