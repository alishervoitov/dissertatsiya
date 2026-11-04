"""Yagona administrator telefonini ham, zaxira kodlarini ham yo'qotsa - serverdan tiklash.

    docker compose -f docker-compose.prod.yml exec backend python manage.py reset_2fa <username>
"""
import logging

from django.core.management.base import BaseCommand, CommandError

from users import mfa, sessions
from users.models import User
from users.security import revoke_refresh_tokens

security_logger = logging.getLogger("security")


class Command(BaseCommand):
    help = "Foydalanuvchining 2FA sozlamasi va zaxira kodlarini o'chiradi, barcha sessiyalarini yopadi."

    def add_arguments(self, parser):
        parser.add_argument("username")

    def handle(self, *args, username, **opts):
        user = User.objects.filter(username__iexact=username).first()
        if not user:
            raise CommandError(f"Foydalanuvchi topilmadi: {username}")
        mfa.disable(user)
        revoke_refresh_tokens(user)
        sessions.revoke(sessions.active_sessions(user))
        security_logger.warning("2FA server buyrug'i bilan tiklandi: %r", user.username)
        self.stdout.write(self.style.SUCCESS(
            f"{user.username}: 2FA o'chirildi. Keyingi kirishda qaytadan sozlaydi."))
