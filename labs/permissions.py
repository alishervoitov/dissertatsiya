from rest_framework.permissions import BasePermission

from users.models import Role


class IsLab(BasePermission):
    message = "Bu amal faqat laborant uchun ruxsat etilgan."

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.role == Role.LAB)


def can_read_result(user, result):
    """Bemor faqat o'zinikini; shifokor, administrator va laborant istalgan tahlil natijasini o'qiydi.
    Laborant faqat tahlil natijalarini ko'radi, kasallik tarixi va retseptlar unga yopiq."""
    if not user or not user.is_authenticated:
        return False
    if user.role == Role.PATIENT:
        return result.patient.user_id == user.id
    return user.role in (Role.DOCTOR, Role.ADMIN, Role.LAB)


def can_edit_result(user, result):
    return bool(user and user.is_authenticated and user.role == Role.LAB and result.created_by_id == user.id)
