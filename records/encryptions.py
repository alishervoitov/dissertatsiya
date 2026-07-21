"""
Bemorlarning nozik tibbiy ma'lumotlarini (tashxis, davolash, shifokor izohlari,
milliy ID kabi) ma'lumotlar bazasida "at rest" holatda shifrlab saqlash uchun
maydon turlari.

Nima uchun kerak: agar bazaga (masalan db.sqlite3 fayli yoki backup nusxa)
ruxsatsiz kirish bo'lsa ham, shifrlangan matnlar ochilmagan holda qoladi -
faqat ilova FIELD_ENCRYPTION_KEY orqali ularni o'qiy oladi.

Fernet (AES-128-CBC + HMAC) - simmetrik shifrlash, tez va ishonchli.
"""

from django.conf import settings
from django.db import models
from cryptography.fernet import Fernet, InvalidToken, MultiFernet


def _get_fernet():
    key = settings.FIELD_ENCRYPTION_KEY
    if not key:
        raise RuntimeError(
            "FIELD_ENCRYPTION_KEY sozlanmagan. .env faylida uni generatsiya qiling:\n"
            "python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )
    keys = [k.strip() for k in key.split(",") if k.strip()]
    return MultiFernet([Fernet(k.encode()) for k in keys])


class EncryptedTextField(models.TextField):
    """Uzun matnlar (tashxis, davolash tarixi, shifokor izohlari) uchun."""

    description = "Bazada shifrlangan holda saqlanadigan matn maydoni"

    def get_prep_value(self, value):
        if value is None or value == "":
            return value
        f = _get_fernet()
        return f.encrypt(str(value).encode()).decode()

    def from_db_value(self, value, expression, connection):
        if value is None or value == "":
            return value
        f = _get_fernet()
        try:
            return f.decrypt(value.encode()).decode()
        except InvalidToken:
            # Eski, shifrlanmagan ma'lumot yoki noto'g'ri kalit
            return "[SHIFRNI OCHIB BO'LMADI]"

    def to_python(self, value):
        return value


class EncryptedCharField(models.CharField):
    """Qisqa maxfiy qiymatlar (milliy ID, pasport raqami) uchun."""

    description = "Bazada shifrlangan holda saqlanadigan qisqa matn maydoni"

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("max_length", 500)  # shifrlangach uzunroq bo'ladi
        super().__init__(*args, **kwargs)

    def get_prep_value(self, value):
        if value is None or value == "":
            return value
        f = _get_fernet()
        return f.encrypt(str(value).encode()).decode()

    def from_db_value(self, value, expression, connection):
        if value is None or value == "":
            return value
        f = _get_fernet()
        try:
            return f.decrypt(value.encode()).decode()
        except InvalidToken:
            return "[SHIFRNI OCHIB BO'LMADI]"

    def to_python(self, value):
        return value