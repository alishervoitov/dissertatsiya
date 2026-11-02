
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from appointments.models import DoctorProfile

from .models import Role, User


class DoctorProfileInline(admin.StackedInline):
    model = DoctorProfile
    can_delete = False
    verbose_name_plural = "Qabul sozlamalari (navbat)"


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    list_display = ["username", "email", "first_name", "last_name", "role", "is_active"]
    list_filter = ["role", "is_active"]
    fieldsets = UserAdmin.fieldsets + (
        ("Qo'shimcha ma'lumot", {"fields": ("role", "phone", "is_active_2fa")}),
    )

    def get_inlines(self, request, obj):
        # Mutaxassislik faqat shifokor hisobida ko'rinadi
        return [DoctorProfileInline] if obj and obj.role == Role.DOCTOR else []
