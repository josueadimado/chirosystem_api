from django.apps import AppConfig


class ClinicConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.clinic"
    label = "clinic"

    def ready(self):
        # Connect cache-invalidation signals so ClinicSettings / Service / Provider
        # saves automatically clear the appropriate Redis keys.
        import apps.clinic.signals  # noqa: F401
