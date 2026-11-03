"""
Yuklanayotgan fayl turini brauzer yuborgan Content-Type sarlavhasiga emas, faylning
boshidagi imzo baytlariga (magic bytes) qarab aniqlash. Sarlavhani foydalanuvchi
to'liq nazorat qiladi, shuning uchun unga ishonib bo'lmaydi (CWE-434).
"""

SIGNATURES = {
    "application/pdf": [b"%PDF-"],
    "image/png": [b"\x89PNG\r\n\x1a\n"],
    "image/jpeg": [b"\xff\xd8\xff"],
    "image/webp": [b"RIFF"],  # qo'shimcha: 8-12 baytlar "WEBP" bo'lishi shart
}

EXTENSIONS = {
    "application/pdf": "pdf",
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
}


def detect_type(uploaded):
    """Fayl imzosiga ko'ra MIME turini qaytaradi yoki None (noma'lum/ruxsat etilmagan)."""
    head = uploaded.read(16)
    uploaded.seek(0)
    for ctype, sigs in SIGNATURES.items():
        if any(head.startswith(s) for s in sigs):
            if ctype == "image/webp" and head[8:12] != b"WEBP":
                continue
            return ctype
    return None
