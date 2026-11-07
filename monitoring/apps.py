from django.apps import AppConfig


class MonitoringConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "monitoring"
    verbose_name = "Xavfsizlik monitoringi"

    def ready(self):
        from . import signals  # noqa: F401
