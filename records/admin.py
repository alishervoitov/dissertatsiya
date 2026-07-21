from django.contrib import admin

from .models import AuditLog, MedicalRecord, Patient


@admin.register(Patient)
class PatientAdmin(admin.ModelAdmin):
    list_display = ["id", "user", "gender", "primary_doctor"]
    search_fields = ["user__username", "user__first_name", "user__last_name"]


@admin.register(MedicalRecord)
class MedicalRecordAdmin(admin.ModelAdmin):
    list_display = ["id", "patient", "record_type", "title", "created_by", "visit_date"]
    list_filter = ["record_type"]


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ["id", "timestamp", "actor", "action", "patient", "ip_address"]
    list_filter = ["action"]
    readonly_fields = [f.name for f in AuditLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False