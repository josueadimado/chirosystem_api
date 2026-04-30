from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.shortcuts import render
from django.urls import path, reverse

from .models import (
    Appointment,
    ClinicSettings,
    Invoice,
    Patient,
    Payment,
    Provider,
    ProviderUnavailability,
    Service,
    Visit,
    VisitRenderedService,
)
from .twilio_sms import send_sms_detailed, twilio_configured
from .utils import validate_phone


class TwilioTestSmsForm(forms.Form):
    """Admin-only form to send a one-off SMS via Twilio (for troubleshooting)."""

    phone = forms.CharField(
        label="To (phone number)",
        max_length=32,
        help_text="US or international digits; validated and converted to E.164 before send.",
    )
    message = forms.CharField(
        label="Message",
        widget=forms.Textarea(attrs={"rows": 4, "cols": 40}),
        max_length=1600,
        initial="Relief Chiropractic: Test SMS from admin. Reply STOP to opt out.",
    )


@admin.register(ClinicSettings)
class ClinicSettingsAdmin(admin.ModelAdmin):
    """Single clinic-wide settings (header for bills) + link to Twilio SMS test tool."""

    change_form_template = "admin/clinic/clinicsettings/change_form.html"
    change_list_template = "admin/clinic/clinicsettings/change_list.html"
    list_display = ("clinic_name", "phone", "updated_at")
    readonly_fields = ("created_at", "updated_at")

    fieldsets = (
        (None, {"fields": ("clinic_name", "phone", "email")}),
        ("Address (printed materials)", {"fields": ("address_line1", "city_state_zip")}),
        ("Billing / POS", {"fields": ("pos_default", "no_show_fee")}),
        ("Business hours (JSON)", {"fields": ("business_hours",)}),
        ("Timestamps", {"fields": ("created_at", "updated_at")}),
    )

    def has_add_permission(self, request):
        # One-time: allow creating the singleton if the table is empty.
        return not ClinicSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "send-test-sms/",
                self.admin_site.admin_view(self.send_test_sms_view),
                name="clinic_clinicsettings_send_test_sms",
            ),
        ]
        return custom + urls

    def _send_test_sms_url(self):
        return reverse("admin:clinic_clinicsettings_send_test_sms")

    def change_view(self, request, object_id, form_url="", extra_context=None):
        extra_context = extra_context or {}
        extra_context["send_test_sms_url"] = self._send_test_sms_url()
        return super().change_view(request, object_id, form_url, extra_context=extra_context)

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context["send_test_sms_url"] = self._send_test_sms_url()
        return super().changelist_view(request, extra_context=extra_context)

    def send_test_sms_view(self, request):
        """Staff-only page: send one SMS using server Twilio credentials."""
        opts = self.model._meta
        s = settings
        msg_svc = (getattr(s, "TWILIO_MESSAGING_SERVICE_SID", None) or "").strip()
        from_num = (getattr(s, "TWILIO_PHONE_NUMBER", None) or "").strip()
        if msg_svc:
            from_display = f"Messaging service {msg_svc}"
        elif from_num:
            from_display = from_num
        else:
            from_display = "(not configured)"

        if request.method == "POST":
            form = TwilioTestSmsForm(request.POST)
            if form.is_valid():
                ok_num, e164_or_err = validate_phone(form.cleaned_data["phone"])
                if not ok_num:
                    messages.error(request, e164_or_err)
                else:
                    body = form.cleaned_data["message"].strip()
                    sid, err = send_sms_detailed(to_e164=e164_or_err, body=body)
                    if sid:
                        messages.success(
                            request,
                            "SMS accepted by Twilio. Message SID: %(sid)s. "
                            "Check the phone and the Twilio message log for delivery status." % {"sid": sid},
                        )
                    elif err:
                        messages.error(request, f"Twilio did not send the message: {err}")
                    else:
                        messages.error(request, "SMS was not sent (unknown reason).")
        else:
            form = TwilioTestSmsForm()

        solo = ClinicSettings.get_solo()
        context = {
            **self.admin_site.each_context(request),
            "title": "Send test SMS (Twilio)",
            "opts": opts,
            "form": form,
            "from_display": from_display,
            "twilio_ready": twilio_configured(),
            "clinic_settings_change_url": reverse(
                "admin:clinic_clinicsettings_change", args=[solo.pk]
            ),
        }
        return render(request, "admin/clinic/clinicsettings/send_test_sms.html", context)


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
