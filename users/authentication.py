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
