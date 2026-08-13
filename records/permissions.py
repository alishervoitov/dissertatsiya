from rest_framework.permissions import SAFE_METHODS, BasePermission

from users.models import Role


class PatientObjectPermission(BasePermission):
    """
    Bemor faqat o'z profilini ko'rishi mumkin.
    Shifokor va administrator har qanday bemor profilini ko'rishi mumkin
    (davolash uchun zarur), lekin bu har doim AuditLog ga yoziladi.
    """

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        user = request.user
        if user.role == Role.PATIENT:
            return obj.user_id == user.id and request.method in SAFE_METHODS
        if user.role == Role.DOCTOR:
            return request.method in SAFE_METHODS or request.method in ("PUT", "PATCH")
        if user.role == Role.ADMIN:
            return True
        return False


class MedicalRecordObjectPermission(BasePermission):
    """
    - Bemor: faqat o'ziga tegishli yozuvlarni o'qiy oladi.
    - Shifokor: barcha bemorlar uchun yozuv yarata oladi; faqat o'zi yozgan
      yozuvlarni tahrirlashi/o'chirishi mumkin.
    - Administrator: nazorat maqsadida o'qiy oladi, o'zgartira olmaydi.
    """

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.method == "POST":
            return request.user.role == Role.DOCTOR
        return True

    def has_object_permission(self, request, view, obj):
        user = request.user
        if user.role == Role.PATIENT:
            return obj.patient.user_id == user.id and request.method in SAFE_METHODS
        if user.role == Role.DOCTOR:
            if request.method in SAFE_METHODS:
                return True
            return obj.created_by_id == user.id
        if user.role == Role.ADMIN:
            return request.method in SAFE_METHODS
        return False


class PrescriptionObjectPermission(BasePermission):
    """
    - Bemor: faqat o'ziga tayinlangan retseptlarni o'qiy oladi.
    - Shifokor: barcha bemorlar uchun retsept yoza oladi; faqat o'zi
      yozgan retseptlarni tahrirlashi/o'chirishi mumkin.
    - Administrator: faqat nazorat uchun o'qiy oladi.
    """

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.method == "POST":
            return request.user.role == Role.DOCTOR
        return True

    def has_object_permission(self, request, view, obj):
        user = request.user
        if user.role == Role.PATIENT:
            return obj.patient.user_id == user.id and request.method in SAFE_METHODS
        if user.role == Role.DOCTOR:
            if request.method in SAFE_METHODS:
                return True
            return obj.prescribed_by_id == user.id
        if user.role == Role.ADMIN:
            return request.method in SAFE_METHODS
        return False