"""
Shifoxonaga kelganlikni QR kod orqali tasdiqlash.

Ikki xil kod bor:
  * Aylanuvchi kod (asosiy): kiosk ekranida ko'rsatiladi, har QUEUE_CHECKIN_WINDOW
    soniyada almashadi. Kod = "r.<oyna>.<HMAC>"; joriy va bitta oldingi oyna qabul
    qilinadi, shuning uchun rasmga olib uyga yuborilgan kod tez orada yaroqsiz bo'ladi.
  * Chop etiladigan kod (zaxira): administrator yoqadi, faqat o'sha kun amal qiladi,
    istalgan vaqtda bekor qilinadi. Kod = "p.<tasodifiy satr>".
"""
import hashlib
import hmac
import io
import time
from urllib.parse import urlsplit

from django.conf import settings

ROTATING_PREFIX = "r."
PRINTED_PREFIX = "p."


def window_seconds():
    return int(getattr(settings, "QUEUE_CHECKIN_WINDOW", 45))


def _key():
    # SECRET_KEY dan alohida maqsadli kalit chiqariladi (boshqa imzolar bilan aralashmasin)
    return hashlib.sha256(f"queue-checkin:{settings.SECRET_KEY}".encode()).digest()


def _sign(window):
    return hmac.new(_key(), f"checkin:{window}".encode(), hashlib.sha256).hexdigest()[:20]


def current_window(now=None):
    return int((now if now is not None else time.time()) // window_seconds())


def rotating_code(now=None):
    w = current_window(now)
    expires_in = window_seconds() - int((now if now is not None else time.time()) % window_seconds())
    return f"{ROTATING_PREFIX}{w}.{_sign(w)}", expires_in


def verify_rotating(code, now=None):
    try:
        _, w, sig = code.split(".")
        w = int(w)
    except (ValueError, AttributeError):
        return False
    cur = current_window(now)
    if w not in (cur, cur - 1):
        return False
    return hmac.compare_digest(sig, _sign(w))


def safe_base(request, base):
    """QR ichidagi havola uchun sayt manzili. Faqat shu so'rov kelgan host yoki
    FRONTEND_URL qabul qilinadi, aks holda QR boshqa saytga olib ketishi mumkin edi."""
    allowed = {request.get_host().split(":")[0]}
    front = getattr(settings, "FRONTEND_URL", "")
    if front:
        allowed.add(urlsplit(front).hostname or "")
    if base:
        parts = urlsplit(base)
        if parts.scheme in ("http", "https") and parts.hostname in allowed:
            return f"{parts.scheme}://{parts.netloc}"
    return front.rstrip("/")


def checkin_url(base, code):
    return f"{base}/arrive?c={code}"


def qr_svg(text):
    """Matndan QR kodli SVG yasaydi (reportlab ichidagi QR kodlovchi, qo'shimcha kutubxonasiz)."""
    from reportlab.graphics.barcode.qr import QrCodeWidget

    widget = QrCodeWidget(text, barLevel="M")
    qr = widget.qr
    qr.make()
    n = qr.getModuleCount()
    border = 4
    size = n + border * 2
    out = io.StringIO()
    out.write(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" shape-rendering="crispEdges">')
    out.write(f'<rect width="{size}" height="{size}" fill="#fff"/><path fill="#000" d="')
    for r in range(n):
        for c in range(n):
            if qr.isDark(r, c):
                out.write(f"M{c + border} {r + border}h1v1h-1z")
    out.write('"/></svg>')
    return out.getvalue()
