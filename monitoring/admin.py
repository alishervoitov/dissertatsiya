from django.contrib import admin

from .models import SecurityAlert


@admin.register(SecurityAlert)
class SecurityAlertAdmin(admin.ModelAdmin):
    list_display = ["last_seen", "rule", "severity", "actor", "count", "status"]
    list_filter = ["rule", "severity", "status"]
    readonly_fields = [f.name for f in SecurityAlert._meta.fields]
