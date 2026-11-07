"""Django admin (/django-admin/) kirishi: parol + 2FA kodi + bloklash.

Django admin sukut bo'yicha faqat parol so'raydi va DRF'dagi 2FA va login bloklashni chetlab
o'tadi - bu esa butun bazaga to'g'ridan-to'g'ri kirish. Shuning uchun:
  * faqat 2FA yoqilgan xodim kira oladi (Google Authenticator kodi yoki zaxira kod);
  * noto'g'ri parol/kod saytdagi login bilan bir xil hisoblagichga tushadi (5 urinish -> bloklash);
  * har bir urinish xavfsizlik jurnaliga yoziladi;
  * DJANGO_ADMIN_ENABLED=0 bo'lsa /django-admin/ umuman ochilmaydi (config/urls.py).
"""
import logging

from django import forms
from django.contrib.admin.forms import AdminAuthenticationForm
from django.core.exceptions import ValidationError

from . import mfa
from .security import clear_login_failures, client_ident, is_login_locked, register_login_failure

security_logger = logging.getLogger("security")

FAILED = "Login, parol yoki kod noto'g'ri."


class MfaAdminAuthenticationForm(AdminAuthenticationForm):
    otp = forms.CharField(
        label="2FA kodi", max_length=11, strip=True,
        widget=forms.TextInput(attrs={"autocomplete": "one-time-code", "inputmode": "numeric", "placeholder": "000000"}),
        help_text="Google Authenticator'dagi 6 xonali kod yoki zaxira kod.",
    )

    def clean(self):
        username = self.cleaned_data.get("username")
        ident = client_ident(self.request)
        if is_login_locked(username, ident):
            security_logger.warning("Django admin: bloklangan hisobga urinish %r (IP: %s)", username, ident)
            raise ValidationError(FAILED, code="invalid_login")
        try:
            super().clean()                      # parol + is_staff/is_active tekshiruvi
        except ValidationError:
            self._fail(username, ident, "parol")
            raise ValidationError(FAILED, code="invalid_login")

        user = self.get_user()
        if not mfa.enabled_device(user):
            security_logger.warning("Django admin: 2FA yoqilmagan hisob rad etildi %r (IP: %s)", username, ident)
            raise ValidationError(
                "Django admin faqat ikki bosqichli kirish yoqilgan hisoblar uchun. "
                "Avval saytga kirib «Ikki bosqichli kirish»ni sozlang.", code="mfa_required")
        if not mfa.verify_any(user, self.cleaned_data.get("otp")):
            self._fail(username, ident, "2FA kodi")
            raise ValidationError(FAILED, code="invalid_login")

        clear_login_failures(username, ident)
        security_logger.info("Django admin: muvaffaqiyatli kirish (2FA) %r (IP: %s)", username, ident)
        return self.cleaned_data

    def _fail(self, username, ident, what):
        locked = register_login_failure(username, ident)
        security_logger.warning("Django admin: noto'g'ri %s %r (IP: %s)%s", what, username, ident,
                                " - bloklandi" if locked else "")
        if locked:
            from monitoring.notify import login_locked
            from .security import LOCKOUT_DURATION
            login_locked(username, ident, int(LOCKOUT_DURATION.total_seconds() // 60), source="Django admin")
