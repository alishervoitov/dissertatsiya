from rest_framework.exceptions import PermissionDenied
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken

from .sessions import SID_CLAIM, is_revoked


class SessionJWTAuthentication(JWTAuthentication):
    """Oddiy JWT tekshiruvi + yopilgan sessiyaning access tokeni darhol rad etiladi."""

    def get_validated_token(self, raw_token):
        token = super().get_validated_token(raw_token)
        if is_revoked(token.get(SID_CLAIM)):
            raise InvalidToken("Sessiya yopilgan. Qaytadan kiring.")
        return token

    # 2FA sozlanmaguncha majburiy rol (admin, shifokor) faqat shu manzillarga kira oladi
    MFA_SETUP_ALLOWED = ("/api/auth/me/", "/api/auth/2fa/", "/api/auth/logout/")

    def authenticate(self, request):
        result = super().authenticate(request)
        if result is not None:
            from .mfa import required_for
            user = result[0]
            if required_for(user) and not user.is_active_2fa and not request.path.startswith(self.MFA_SETUP_ALLOWED):
                raise PermissionDenied({
                    "detail": "Ikki bosqichli kirishni sozlang: rolingiz uchun u majburiy.",
                    "code": "mfa_setup_required",
                })
        return result
