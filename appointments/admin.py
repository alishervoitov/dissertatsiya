from django.contrib import admin

from .models import DoctorProfile, QueueTicket


@admin.register(DoctorProfile)
class DoctorProfileAdmin(admin.ModelAdmin):
    list_display = ["user", "specialty", "room", "work_start", "work_end", "avg_minutes", "accepts_queue"]
    list_editable = ["specialty", "room", "work_start", "work_end", "avg_minutes", "accepts_queue"]
    search_fields = ["user__username", "user__first_name", "user__last_name", "specialty"]


@admin.register(QueueTicket)
class QueueTicketAdmin(admin.ModelAdmin):
    list_display = ["date", "number", "doctor", "source", "status", "created_at"]
    list_filter = ["date", "source", "status"]
    readonly_fields = ["created_at", "called_at", "finished_at"]
