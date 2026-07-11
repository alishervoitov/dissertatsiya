from rest_framework.permissions import BasePermission

from .models import Role


class IsAdmin(BasePermission):
    message = "Bu amal faqat administrator uchun ruxsat etilgan."

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.role == Role.ADMIN)


class IsDoctor(BasePermission):
    message = "Bu amal faqat shifokor uchun ruxsat etilgan."

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.role == Role.DOCTOR)


class IsPatient(BasePermission):
    message = "Bu amal faqat bemor uchun ruxsat etilgan."

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.role == Role.PATIENT)


class IsAdminOrDoctor(BasePermission):
    message = "Bu amal faqat shifokor yoki administrator uchun ruxsat etilgan."

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.role in (Role.ADMIN, Role.DOCTOR)
        )