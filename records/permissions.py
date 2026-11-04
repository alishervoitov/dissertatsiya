from rest_framework.permissions import SAFE_METHODS, BasePermission

from users.models import Role

# Klinik ma'lumot (kasallik tarixi, retsept, fayllar) faqat shu rollarga ochiq. Yangi rol qo'shilganda
# (masalan, laborant) u avtomatik ruxsat olmasligi uchun ro'yxat "ruxsat berilganlar" tamoyilida.
CLINICAL_ROLES = (Role.PATIENT, Role.DOCTOR, Role.ADMIN)


def can_read_patient(user, patient):
    """Bemorning klinik ma'lumotini o'qish: bemorning o'zi, ROZILIGI bor shifokor yoki administrator."""
    if not user or not user.is_authenticated:
        return False
    if user.role == Role.PATIENT:
        return patient.user_id == user.id
    if user.role == Role.DOCTOR:
        from .consent import doctor_has_access
        return doctor_has_access(user, patient)
    return user.role == Role.ADMIN


class PatientObjectPermission(BasePermission):
    """
    Bemor faqat o'z profilini ko'rishi mumkin.
    Shifokor va administrator har qanday bemor profilini ko'rishi mumkin
    (davolash uchun zarur), lekin bu har doim AuditLog ga yoziladi.
    """

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.role in CLINICAL_ROLES)

    def has_object_permission(self, request, view, obj):
        user = request.user
        if user.role == Role.PATIENT:
            return obj.user_id == user.id and request.method in SAFE_METHODS
        if user.role == Role.DOCTOR:
            from .consent import require_access
            require_access(user, obj)        # rozilik yo'q bo'lsa 403 (code=consent_required)
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
        if request.user.role not in CLINICAL_ROLES:
            return False
        if request.method == "POST":
            return request.user.role == Role.DOCTOR
        return True

    def has_object_permission(self, request, view, obj):
        user = request.user
        if user.role == Role.PATIENT:
            return obj.patient.user_id == user.id and request.method in SAFE_METHODS
        if user.role == Role.DOCTOR:
            from .consent import require_access
            require_access(user, obj.patient)
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
        if request.user.role not in CLINICAL_ROLES:
            return False
        if request.method == "POST":
            return request.user.role == Role.DOCTOR
        return True

    def has_object_permission(self, request, view, obj):
        user = request.user
        if user.role == Role.PATIENT:
            return obj.patient.user_id == user.id and request.method in SAFE_METHODS
        if user.role == Role.DOCTOR:
            from .consent import require_access
            require_access(user, obj.patient)
            if request.method in SAFE_METHODS:
                return True
            return obj.prescribed_by_id == user.id
        if user.role == Role.ADMIN:
            return request.method in SAFE_METHODS
        return False