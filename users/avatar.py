"""
Avatar yuklash: faylni Pillow bilan tekshirish va qayta kodlash (pentest 2026-10-19, 2/3/4-bandlar).

Fayl mazmuni rasm sifatida ochiladi, o'lchami piksellar yuklanishidan oldin tekshiriladi
(decompression bomb), keyin metadatasiz qayta saqlanadi - ichiga yashirilgan narsa yo'qoladi.
"""
import re
import uuid
from io import BytesIO
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from PIL import Image, ImageOps, UnidentifiedImageError

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
FORMAT_EXTENSIONS = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}
MAX_SIDE = 4096
MAX_PIXELS = 16_000_000
OUTPUT_SIZE = (512, 512)

# Faqat shu modul yaratgan nomlar avtomatik o'chiriladi. Eski nomlar (jumladan pentest
# dalillari) kod tomonidan hech qachon o'chirilmaydi - ular qo'lda ko'rib chiqiladi.
GENERATED_NAME_RE = re.compile(r"^avatars/[0-9a-f]{32}\.(jpg|png|webp)$")


class AvatarError(ValueError):
    """Yuklangan fayl avatar sifatida qabul qilinmaydi; xabar foydalanuvchiga ko'rsatiladi."""


def _normalize_mode(img, fmt):
    """Rejimni chiqish formatiga moslaydi: JPEG -> RGB/L, PNG/WEBP -> shaffoflik saqlanadi."""
    has_alpha = img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info
    if fmt == "JPEG":
        if has_alpha:
            rgba = img.convert("RGBA")
            background = Image.new("RGB", rgba.size, (255, 255, 255))
            background.paste(rgba, mask=rgba.getchannel("A"))
            return background
        return img if img.mode in ("RGB", "L") else img.convert("RGB")
    if has_alpha:
        return img if img.mode == "RGBA" else img.convert("RGBA")
    if fmt == "PNG" and img.mode == "L":
        return img
    return img if img.mode == "RGB" else img.convert("RGB")


def process_avatar(uploaded):
    """Faylni tekshirib qayta kodlaydi va (ContentFile, tasodifiy nom) qaytaradi; yaroqsiz bo'lsa AvatarError."""
    if uploaded.size > settings.AVATAR_MAX_BYTES:
        raise AvatarError(f"Fayl hajmi {settings.AVATAR_MAX_BYTES // 1_000_000} MB dan oshmasligi kerak.")
    if Path(uploaded.name or "").suffix.lower() not in ALLOWED_EXTENSIONS:
        raise AvatarError("Faqat JPEG, PNG yoki WEBP rasm qabul qilinadi.")

    try:
        with Image.open(uploaded) as img:
            fmt = img.format
            if fmt not in FORMAT_EXTENSIONS:
                raise AvatarError("Faqat JPEG, PNG yoki WEBP rasm qabul qilinadi.")
            width, height = img.size
            if width > MAX_SIDE or height > MAX_SIDE or width * height > MAX_PIXELS:
                raise AvatarError("Rasm o'lchami juda katta.")
            img.verify()

        # verify() dan keyin rasmni qayta ochish kerak
        uploaded.seek(0)
        with Image.open(uploaded) as img:
            img.load()
            img = ImageOps.exif_transpose(img)
            img = _normalize_mode(img, fmt)
            img.thumbnail(OUTPUT_SIZE)
            # Pillow save() da info'dagi comment/icc_profile kabi maydonlarni yangi faylga
            # ko'chiradi - ular orqali payload saqlanib qolmasligi uchun tozalanadi.
            img.info = {}
            out = BytesIO()
            if fmt == "JPEG":
                img.save(out, "JPEG", quality=85, optimize=True)
            elif fmt == "PNG":
                img.save(out, "PNG", optimize=True)
            else:
                img.save(out, "WEBP", quality=85)
    except AvatarError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError, ValueError) as exc:
        raise AvatarError("Fayl yaroqli rasm emas.") from exc

    return ContentFile(out.getvalue()), f"{uuid.uuid4().hex}.{FORMAT_EXTENSIONS[fmt]}"


def delete_old_avatar(storage, name):
    """Eski avatarni o'chiradi, lekin faqat u shu modul yaratgan nom bo'lsa."""
    if name and GENERATED_NAME_RE.match(name):
        storage.delete(name)
