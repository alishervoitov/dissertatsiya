import logging

from celery import shared_task
from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from .security import mask_email

security_logger = logging.getLogger("security")


@shared_task
def request_password_reset(email):
    """
    Email bo'yicha hisobni qidiradi va topilsa tiklash havolasini yuboradi.
    Qidiruv view'da emas, shu yerda: API javob vaqti email mavjudligiga bog'liq
    bo'lmasligi uchun (pentest 2026-10-19, 5-band).
    """
    from .models import User

    # Faqat tasdiqlangan email: aks holda birovning emailini yozgan foydalanuvchi orqali
    # tiklash xati begona odamga ketadi (users/email_verify.py)
    user = User.objects.filter(email__iexact=email, email_verified=True, is_active=True).first()
    if not user:
        # Email foydalanuvchi kiritgan qiymat: niqoblanadi (shaxsiy ma'lumot, log injection)
        security_logger.info("Parolni tiklash so'ralgan, lekin email topilmadi: %s", mask_email(email))
        return

    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    reset_link = f"{settings.FRONTEND_URL}/reset-password/{uid}/{token}/"
    send_password_reset_email(user.email, user.get_full_name() or user.username, reset_link)
    security_logger.info("Parolni tiklash havolasi yuborildi: %s", user.username)


@shared_task
def send_password_reset_email(user_email, full_name, reset_link):
    """
    Parolni tiklash havolasini fon rejimida yuboradi - shunda foydalanuvchi
    email serveri sekin javob bersa ham, API so'rovi darhol javob qaytaradi.
    """
    send_mail(
        subject="ShifoSafe — Parolni tiklash",
        message=(
            f"Salom, {full_name}!\n\n"
            f"Parolingizni tiklash uchun quyidagi havolaga o'ting:\n{reset_link}\n\n"
            f"Agar buni siz so'ramagan bo'lsangiz, bu xabarni e'tiborsiz qoldiring."
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user_email],
    )

@shared_task
def send_verification_email(user_email, full_name, code):
    """Emailni tasdiqlash kodi. Xatda tibbiy ma'lumot yo'q, faqat kod."""
    send_mail(
        subject="ShifoSafe — emailni tasdiqlash kodi",
        message=(
            f"Salom, {full_name}!\n\n"
            f"Emailingizni tasdiqlash kodi: {code}\n"
            f"Kod 15 daqiqa amal qiladi.\n\n"
            f"Agar ShifoSafe tizimida ro'yxatdan o'tmagan bo'lsangiz, bu xatni e'tiborsiz qoldiring."
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user_email],
    )


@shared_task
def send_security_notice(user_email, full_name, subject, body):
    """Hisob xavfsizligi haqida ogohlantirish (parol yoki email o'zgardi). Xatda tibbiy ma'lumot yo'q."""
    send_mail(
        subject=f"ShifoSafe — {subject}",
        message=(
            f"Salom, {full_name}!\n\n{body}\n\n"
            f"Agar buni siz qilmagan bo'lsangiz, darhol parolingizni tiklang va registraturaga murojaat qiling."
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user_email],
    )
