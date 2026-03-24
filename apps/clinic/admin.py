from django.contrib import admin

from .models import (
    Appointment,
    Invoice,
    Patient,
    Payment,
    Provider,
    ProviderUnavailability,
    Service,
    Visit,
    VisitRenderedService,
)


@admin.register(Provider)
class ProviderAdmin(admin.ModelAdmin):
    """Doctor profile: linked login user, calendar, bookable services, alerts."""

    list_display = (
        "id",
        "user",
        "title",
        "specialty",
        "active",
        "notification_phone",
        "created_at",
    )
    list_filter = ("active",)
    search_fields = (
        "user__username",
        "user__email",
        "user__full_name",
        "title",
        "specialty",
    )
    autocomplete_fields = ("user",)
    filter_horizontal = ("services",)
    readonly_fields = ("created_at", "updated_at")
    fieldsets = (
        (None, {"fields": ("user", "active")}),
        ("Profile", {"fields": ("title", "specialty", "notification_phone")}),
        (
            "Google Calendar",
            {
                "fields": ("google_refresh_token", "google_calendar_id"),
                "description": "OAuth tokens are set when the doctor connects their calendar in the app.",
            },
        ),
        ("Online booking", {"fields": ("services",)}),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )


admin.site.register(Patient)
admin.site.register(ProviderUnavailability)
admin.site.register(Service)
admin.site.register(Appointment)
admin.site.register(Visit)
admin.site.register(VisitRenderedService)
admin.site.register(Invoice)
admin.site.register(Payment)
