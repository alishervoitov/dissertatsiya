from django.contrib import admin

from .models import LabAttachment, LabResult, LabValue


class LabValueInline(admin.TabularInline):
    model = LabValue
    extra = 0


@admin.register(LabResult)
class LabResultAdmin(admin.ModelAdmin):
    list_display = ["id", "patient", "panel", "title", "collected_at", "created_by"]
    list_filter = ["panel"]
    inlines = [LabValueInline]


admin.site.register(LabAttachment)
