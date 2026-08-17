from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail


@shared_task
def send_password_reset_email(user_email, full_name, reset_link):
    """
    Parolni tiklash havolasini fon rejimida yuboradi - shunda foydalanuvchi
    email serveri sekin javob bersa ham, API so'rovi darhol javob qaytaradi.
    """
    send_mail(
        subject="MedKarta — Parolni tiklash",
        message=(
            f"Salom, {full_name}!\n\n"
            f"Parolingizni tiklash uchun quyidagi havolaga o'ting:\n{reset_link}\n\n"
            f"Agar buni siz so'ramagan bo'lsangiz, bu xabarni e'tiborsiz qoldiring."
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[user_email],
    )