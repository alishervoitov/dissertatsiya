from django.apps import AppConfig


class RecordsConfig(AppConfig):
    name = 'records'

from django.apps import AppConfig

class RecordsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'records'

    def ready(self):
        from . import signals  # noqa: F401
