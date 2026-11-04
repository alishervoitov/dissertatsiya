"""Bemorning 3x4 rasmi: tekshirish, kesish va qayta kodlash.

Avatar bilan bir xil himoya (users/avatar.py): fayl Pillow bilan rasm sifatida ochiladi,
o'lchami piksellar yuklanishidan oldin tekshiriladi (decompression bomb), EXIF bo'yicha
buriladi va metadatasiz qayta saqlanadi - GPS koordinatalar, telefon modeli va ichiga
yashirilgan narsa yo'qoladi. Natija har doim 600x800 JPEG (3:4).
"""
import base64
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from users.avatar import ALLOWED_EXTENSIONS, FORMAT_EXTENSIONS, _normalize_mode

MAX_BYTES = 10_000_000          # telefon kamerasi rasmlari 3-8 MB bo'ladi
MAX_SIDE = 8000
MAX_PIXELS = 50_000_000
OUTPUT = (600, 800)             # 3x4
RATIO = OUTPUT[0] / OUTPUT[1]


class PhotoError(ValueError):
    """Foydalanuvchiga ko'rsatiladigan xato."""


def _fraction(value, default):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(v, 0.0), 1.0)


def crop_box(width, height, crop=None):
    """Kesish sohasi (piksel). crop = {x, y, w, h} - rasm o'lchamiga nisbatan 0..1 ulushlar.
    Har qanday holatda natija 3:4 bo'ladi va rasm chegarasidan chiqmaydi."""
    crop = crop or {}
    if crop.get("w") not in (None, ""):
        x, y = _fraction(crop.get("x"), 0) * width, _fraction(crop.get("y"), 0) * height
        w, h = _fraction(crop.get("w"), 1) * width, _fraction(crop.get("h"), 1) * height
        cx, cy = x + w / 2, y + h / 2
    else:
        w, h, cx, cy = width, height, width / 2, height / 2
    # 3:4 ga keltirish (kichrayadi, kattalashmaydi)
    if w / max(h, 1) > RATIO:
        w = h * RATIO
    else:
        h = w / RATIO
    w, h = max(min(w, width, height * RATIO), 30), max(min(h, height, width / RATIO), 40)
    left = min(max(cx - w / 2, 0), width - w)
    top = min(max(cy - h / 2, 0), height - h)
    return round(left), round(top), round(left + w), round(top + h)


def process_photo(uploaded, crop=None):
    """Yuklangan faylni tekshirib, 600x800 JPEG baytlarini qaytaradi; yaroqsiz bo'lsa PhotoError."""
    if uploaded.size > MAX_BYTES:
        raise PhotoError(f"Fayl hajmi {MAX_BYTES // 1_000_000} MB dan oshmasligi kerak.")
    if Path(uploaded.name or "").suffix.lower() not in ALLOWED_EXTENSIONS:
        raise PhotoError("Faqat JPEG, PNG yoki WEBP rasm qabul qilinadi.")
    try:
        with Image.open(uploaded) as img:
            if img.format not in FORMAT_EXTENSIONS:
                raise PhotoError("Faqat JPEG, PNG yoki WEBP rasm qabul qilinadi.")
            w, h = img.size
            if w > MAX_SIDE or h > MAX_SIDE or w * h > MAX_PIXELS:
                raise PhotoError("Rasm o'lchami juda katta.")
            img.verify()
        uploaded.seek(0)
        with Image.open(uploaded) as img:
            img.load()
            img = ImageOps.exif_transpose(img)
            img = _normalize_mode(img, "JPEG")
            if img.mode != "RGB":
                img = img.convert("RGB")
            if img.width < 120 or img.height < 160:
                raise PhotoError("Rasm juda kichik (kamida 120x160).")
            img = img.crop(crop_box(img.width, img.height, crop)).resize(OUTPUT, Image.LANCZOS)
            img.info = {}
            out = BytesIO()
            img.save(out, "JPEG", quality=86, optimize=True)
    except PhotoError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError, ValueError) as exc:
        raise PhotoError("Fayl yaroqli rasm emas.") from exc
    return out.getvalue()


def encode(data):
    return base64.b64encode(data).decode()


def decode(text):
    try:
        return base64.b64decode(text, validate=True)
    except (ValueError, TypeError):
        return None
