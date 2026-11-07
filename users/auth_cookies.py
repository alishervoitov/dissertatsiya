"""Refresh token - httpOnly cookie'da (S6, I7 tahdidlari).

Ilgari ikkala token ham brauzerning localStorage'ida turardi: sahifadagi har qanday XSS ularni
o'qib, tashqariga yuborishi mumkin edi. Endi:
  * refresh token (1 kun) - faqat httpOnly cookie: JavaScript uni umuman ko'rmaydi;
  * access token (15 daqiqa) - faqat sahifa xotirasida (o'zgaruvchida), diskka yozilmaydi;
  * cookie faqat /api/auth/ ga yuboriladi (boshqa API so'rovlarida yo'q), SameSite=Strict,
    HTTPS'da Secure.

Cookie avtomatik yuborilgani uchun CSRF xavfi paydo bo'ladi. Ikki to'siq:
  1) SameSite=Strict - boshqa saytdan kelgan so'rovga cookie qo'shilmaydi;
  2) cookie o'qiydigan endpointlar (refresh, logout) "X-Requested-With: XMLHttpRequest"
     sarlavhasini talab qiladi - oddiy forma uni yubora olmaydi, boshqa manbadan esa
     CORS preflight'dan o'tmaydi.
"""
from django.conf import settings

CSRF_HEADER = "HTTP_X_REQUESTED_WITH"
CSRF_VALUE = "XMLHttpRequest"


def _name():
    return getattr(settings, "AUTH_REFRESH_COOKIE", "sm_refresh")


def _path():
    return getattr(settings, "AUTH_REFRESH_COOKIE_PATH", "/api/auth/")


def set_refresh_cookie(response, refresh):
    lifetime = settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"]
    response.set_cookie(
        _name(), str(refresh), max_age=int(lifetime.total_seconds()), path=_path(),
        secure=getattr(settings, "AUTH_COOKIE_SECURE", False), httponly=True, samesite="Strict",
    )
    return response


def clear_refresh_cookie(response):
    response.delete_cookie(_name(), path=_path(), samesite="Strict")
    return response


def move_refresh_to_cookie(response):
    """Javob tanasidagi refresh tokenni olib tashlab, cookie qilib qo'yadi."""
    refresh = response.data.pop("refresh", None) if isinstance(response.data, dict) else None
    if refresh:
        set_refresh_cookie(response, refresh)
    return response


def read_refresh(request):
    return request.COOKIES.get(_name()) or None


def csrf_header_ok(request):
    return request.META.get(CSRF_HEADER) == CSRF_VALUE
