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
        ("Profile", {"fields": ("title", "specialty", "primary_service_type", "notification_phone")}),
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


@admin.register(Patient)
class PatientAdmin(admin.ModelAdmin):
    """Demographics, Square card hints, SMS consent, and online-booking intake waiver for imports."""

    list_display = (
        "id",
        "last_name",
        "first_name",
        "phone",
        "online_chiro_intake_waived",
        "sms_consent",
        "updated_at",
    )
    list_filter = ("online_chiro_intake_waived", "sms_consent")
    search_fields = ("first_name", "last_name", "phone", "email")
    readonly_fields = ("created_at", "updated_at")
    fieldsets = (
        (None, {"fields": ("first_name", "last_name", "phone", "email", "date_of_birth")}),
        ("Address & emergency", {"fields": ("address_line1", "address_line2", "city_state_zip", "emergency_contact_name", "emergency_contact_phone")}),
        ("Square (card on file hints)", {"fields": ("square_customer_id", "square_card_id", "card_brand", "card_last4")}),
        (
            "Online booking",
            {
                "fields": ("online_chiro_intake_waived", "sms_consent", "sms_consent_at"),
                "description": "Check “Waive online chiro intake rule” for migrated or established patients who should book regular visits without a completed visit already in this system.",
            },
        ),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )
admin.site.register(ProviderUnavailability)
admin.site.register(Service)
admin.site.register(Appointment)
admin.site.register(Visit)
admin.site.register(VisitRenderedService)
admin.site.register(Invoice)
admin.site.register(Payment)
