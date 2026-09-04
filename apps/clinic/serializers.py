from decimal import Decimal

import logging

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from .patient_phone import duplicate_patient_message, find_duplicate_patient
from .utils import validate_phone
from .models import (
    Appointment,
    DiagnosisCode,
    InsuranceCompany,
    Invoice,
    Patient,
    PatientCreditTransaction,
    Payment,
    Provider,
    ProviderUnavailability,
    Service,
    StaffNotification,
    SystemErrorLog,
    Visit,
    VisitRenderedService,
    VoiceCallLog,
)
from .visit_diagnosis import update_visit_diagnosis_fields

User = get_user_model()
logger = logging.getLogger(__name__)


class PatientSerializer(serializers.ModelSerializer):
    class Meta:
        model = Patient
        # Exclude internal Square payment-processor IDs (square_customer_id, square_card_id)
        # — they are never read or written by the frontend; exposing them over the API is
        # unnecessary and slightly increases response size.
        fields = (
            "id",
            "first_name",
            "last_name",
            "phone",
            "email",
            "date_of_birth",
            "date_established",
            "address_line1",
            "address_line2",
            "city_state_zip",
            "emergency_contact_name",
            "emergency_contact_phone",
            "marital_status",
            "card_brand",
            "card_last4",
            "sms_consent",
            "sms_consent_at",
            "notify_booking",
            "notify_reminders",
            "notify_bills",
            "online_chiro_intake_waived",
            "payment_profile",
            "credit_balance",
            "created_at",
            "updated_at",
        )

    def validate_phone(self, value):
        valid, result = validate_phone(value or "")
        if not valid:
            raise serializers.ValidationError(result)
        return result

    def validate(self, attrs):
        inst = getattr(self, "instance", None)
        if inst is None and not attrs.get("date_of_birth"):
            raise serializers.ValidationError(
                {
                    "date_of_birth": (
                        "Date of birth is required when adding a patient so we can detect duplicates."
                    )
                }
            )

        first_name = attrs.get("first_name", inst.first_name if inst else "")
        last_name = attrs.get("last_name", inst.last_name if inst else "")
        phone = attrs.get("phone", inst.phone if inst else "")
        if "date_of_birth" in attrs:
            date_of_birth = attrs["date_of_birth"]
        else:
            date_of_birth = inst.date_of_birth if inst else None

        if date_of_birth is not None:
            dup = find_duplicate_patient(
                first_name=first_name,
                last_name=last_name,
                phone=phone,
                date_of_birth=date_of_birth,
                exclude_pk=inst.pk if inst else None,
            )
            if dup is not None:
                raise serializers.ValidationError(
                    {"detail": duplicate_patient_message(dup, updating=inst is not None)}
                )

        return attrs


class PatientListSerializer(serializers.ModelSerializer):
    """Lightweight patient row for paginated directory lists (doctors, desk search)."""

    no_show_count = serializers.IntegerField(read_only=True, default=0)
    visit_count = serializers.IntegerField(read_only=True, default=0)
    last_visit = serializers.DateField(read_only=True, allow_null=True)
    last_service = serializers.CharField(read_only=True, allow_null=True)
    next_appointment_date = serializers.DateField(read_only=True, allow_null=True)
    next_appointment_time = serializers.TimeField(read_only=True, allow_null=True)
    date_established = serializers.DateField(
        source="effective_date_established",
        read_only=True,
        allow_null=True,
    )

    class Meta:
        model = Patient
        fields = (
            "id",
            "first_name",
            "last_name",
            "phone",
            "email",
            "date_of_birth",
            "payment_profile",
            "no_show_count",
            "visit_count",
            "last_visit",
            "last_service",
            "next_appointment_date",
            "next_appointment_time",
            "date_established",
        )


class ProviderSerializer(serializers.ModelSerializer):
    provider_name = serializers.CharField(source="user.full_name", read_only=True)
    username = serializers.CharField(source="user.username", read_only=True)
    services = serializers.PrimaryKeyRelatedField(many=True, queryset=Service.objects.all(), required=False)
    # Create a new doctor login (alternative to passing existing user id)
    new_username = serializers.CharField(write_only=True, required=False, allow_blank=True)
    new_password = serializers.CharField(write_only=True, required=False, allow_blank=True, min_length=8)
    new_email = serializers.EmailField(write_only=True, required=False, allow_blank=True)
    new_full_name = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=200)
    # PATCH: update the linked login’s display name (shown everywhere as provider_name)
    display_name = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=200)

    class Meta:
        model = Provider
        fields = (
            "id",
            "user",
            "username",
            "provider_name",
            "title",
            "credential",
            "billing_provider_id",
            "specialty",
            "primary_service_type",
            "active",
            "notification_phone",
            "services",
            "created_at",
            "updated_at",
            "new_username",
            "new_password",
            "new_email",
            "new_full_name",
            "display_name",
        )
        extra_kwargs = {"user": {"required": False, "allow_null": True}}

    def validate_notification_phone(self, value):
        raw = (value or "").strip()
        if not raw:
            return ""
        valid, result = validate_phone(raw)
        if not valid:
            raise serializers.ValidationError(result)
        return result

    def validate_user(self, value):
        if value is None:
            return value
        if Provider.objects.filter(user=value).exists():
            raise serializers.ValidationError("This user already has a provider profile.")
        if value.role != User.Roles.DOCTOR:
            raise serializers.ValidationError("Linked user must have the doctor role.")
        return value

    def validate(self, attrs):
        if self.instance is not None:
            return attrs
        user = attrs.get("user")
        nu = (attrs.get("new_username") or "").strip()
        np = attrs.get("new_password") or ""
        if user is not None:
            if nu or np:
                raise serializers.ValidationError(
                    "Use either an existing user id, OR new username/password for a new doctor — not both."
                )
            return attrs
        if not nu or not np:
            raise serializers.ValidationError(
                "Create a doctor by sending new_username + new_password (and optional new_full_name, new_email), "
                "or send user=<existing doctor user id>."
            )
        if User.objects.filter(username=nu).exists():
            raise serializers.ValidationError({"new_username": "This username is already taken."})
        return attrs

    def create(self, validated_data):
        services = validated_data.pop("services", None)
        nu = (validated_data.pop("new_username", None) or "").strip()
        np = validated_data.pop("new_password", None) or ""
        ne = (validated_data.pop("new_email", None) or "").strip()
        nf = (validated_data.pop("new_full_name", None) or "").strip()

        if nu:
            user = User(
                username=nu,
                email=ne,
                full_name=nf,
                role=User.Roles.DOCTOR,
            )
            user.set_password(np)
            user.save()
            validated_data["user"] = user

        provider = Provider.objects.create(**validated_data)
        if services is not None:
            provider.services.set(services)
        else:
            from apps.accounts.team_helpers import ensure_provider_for_doctor

            ensure_provider_for_doctor(
                provider.user,
                primary_service_type=provider.primary_service_type,
            )
        return provider

    def update(self, instance, validated_data):
        validated_data.pop("new_username", None)
        validated_data.pop("new_password", None)
        validated_data.pop("new_email", None)
        validated_data.pop("new_full_name", None)
        display_name = validated_data.pop("display_name", serializers.empty)
        services = validated_data.pop("services", serializers.empty)
        instance = super().update(instance, validated_data)
        if services is not serializers.empty:
            instance.services.set(services)
        if display_name is not serializers.empty:
            instance.user.full_name = (display_name or "").strip()
            instance.user.save(update_fields=["full_name"])
        return instance

    def to_representation(self, instance):
        rep = super().to_representation(instance)
        if "services" not in rep or rep["services"] is None:
            rep["services"] = list(instance.services.values_list("pk", flat=True))
        return rep


class ServiceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Service
        # Explicit list: `created_at` / `updated_at` are not used by any frontend list or edit view.
        fields = (
            "id",
            "name",
            "public_booking_name",
            "description",
            "duration_minutes",
            "price",
            "billing_code",
            "is_active",
            "show_in_public_booking",
            "visible_to_chiropractic_staff",
            "visible_to_massage_staff",
            "service_type",
            "is_new_client_intake",
            "charges_patient",
        )


class DiagnosisCodeSerializer(serializers.ModelSerializer):
    class Meta:
        model = DiagnosisCode
        # `created_at` / `updated_at` are not used by the diagnoses management page or billing modals.
        fields = ("id", "code", "description", "is_active")


class InsuranceCompanySerializer(serializers.ModelSerializer):
    class Meta:
        model = InsuranceCompany
        fields = (
            "id",
            "name",
            "claim_email",
            "phone",
            "notes",
            "default_plan_type",
            "is_active",
        )

    def validate_name(self, value):
        name = (value or "").strip()
        if not name:
            raise serializers.ValidationError("Company name is required.")
        return name


class StaffNotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = StaffNotification
        fields = ("id", "kind", "message", "appointment", "read_at", "created_at")
        read_only_fields = ("id", "kind", "message", "appointment", "read_at", "created_at")


class ProviderUnavailabilitySerializer(serializers.ModelSerializer):
    """Admin: block a provider from online booking for a date (all day or a window)."""

    provider_name = serializers.SerializerMethodField()

    class Meta:
        model = ProviderUnavailability
        fields = (
            "id",
            "provider",
            "provider_name",
            "block_date",
            "all_day",
            "start_time",
            "end_time",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "provider_name", "created_at", "updated_at")

    def get_provider_name(self, obj):
        return str(obj.provider)

    def validate(self, attrs):
        inst = self.instance
        all_day = attrs.get("all_day", inst.all_day if inst is not None else True)
        if all_day:
            attrs["all_day"] = True
            attrs["start_time"] = None
            attrs["end_time"] = None
            return attrs
        st = attrs.get("start_time", inst.start_time if inst else None)
        et = attrs.get("end_time", inst.end_time if inst else None)
        if st is None or et is None:
            raise serializers.ValidationError(
                {"non_field_errors": "When not blocking the whole day, start_time and end_time are required."}
            )
        if st >= et:
            raise serializers.ValidationError({"end_time": "Must be after start_time."})
        attrs["all_day"] = False
        return attrs


class ProviderUnavailabilityBulkSerializer(serializers.Serializer):
    """Create the same online-booking block on every calendar day in an inclusive date range."""

    provider = serializers.PrimaryKeyRelatedField(queryset=Provider.objects.filter(active=True))
    date_from = serializers.DateField()
    date_to = serializers.DateField()
    all_day = serializers.BooleanField(default=True)
    start_time = serializers.TimeField(required=False, allow_null=True)
    end_time = serializers.TimeField(required=False, allow_null=True)
    weekdays_only = serializers.BooleanField(
        default=False,
        help_text="If true, skip Saturday and Sunday when creating rows.",
    )

    def validate(self, attrs):
        if attrs["date_from"] > attrs["date_to"]:
            raise serializers.ValidationError({"date_to": "Must be on or after the start date."})
        span = (attrs["date_to"] - attrs["date_from"]).days + 1
        max_days = 400
        if span > max_days:
            raise serializers.ValidationError(
                {"date_to": f"Date range cannot exceed {max_days} days ({span} days requested)."},
            )
        all_day = attrs.get("all_day", True)
        if not all_day:
            st = attrs.get("start_time")
            et = attrs.get("end_time")
            if st is None or et is None:
                raise serializers.ValidationError(
                    {"non_field_errors": "When not blocking the whole day, start_time and end_time are required."},
                )
            if st >= et:
                raise serializers.ValidationError({"end_time": "Must be after start_time."})
        return attrs


class AppointmentHandoffNotesSerializer(serializers.Serializer):
    """Update visit reminders & handoff notes on the appointment (not consultation SOAP notes)."""

    appointment_id = serializers.IntegerField(min_value=1)
    clinical_handoff_notes = serializers.CharField(allow_blank=True, max_length=20000)


class AppointmentSoapNotesSerializer(serializers.Serializer):
    """Save consultation (SOAP) notes on the visit while still in progress."""

    appointment_id = serializers.IntegerField(min_value=1)
    doctor_notes = serializers.CharField(allow_blank=True, max_length=50000)


class VoiceCallLogSerializer(serializers.ModelSerializer):
    outcome_label = serializers.SerializerMethodField()

    class Meta:
        model = VoiceCallLog
        fields = (
            "id",
            "call_sid",
            "from_number",
            "transcript",
            "conversation_log",
            "outcome",
            "outcome_label",
            "detail",
            "appointment_id",
            "created_at",
            "updated_at",
        )

    def get_outcome_label(self, obj):
        return obj.get_outcome_display()


class SystemErrorLogListSerializer(serializers.ModelSerializer):
    class Meta:
        model = SystemErrorLog
        fields = (
            "id",
            "created_at",
            "updated_at",
            "level",
            "source",
            "message",
            "exception_type",
            "http_method",
            "path",
            "status_code",
            "user_id",
            "user_display",
            "user_role",
            "resolved_at",
            "resolved_by_id",
            "fingerprint",
        )


class SystemErrorLogGroupListSerializer(SystemErrorLogListSerializer):
    """Latest row per fingerprint, with how many times that error occurred."""

    occurrence_count = serializers.IntegerField(read_only=True)
    first_occurrence_at = serializers.DateTimeField(read_only=True)
    auto_reopened = serializers.SerializerMethodField()

    class Meta(SystemErrorLogListSerializer.Meta):
        fields = SystemErrorLogListSerializer.Meta.fields + (
            "occurrence_count",
            "first_occurrence_at",
            "auto_reopened",
        )

    def get_auto_reopened(self, obj) -> bool:
        return bool((obj.extra or {}).get("auto_reopened"))


class SystemErrorLogDetailSerializer(SystemErrorLogListSerializer):
    class Meta(SystemErrorLogListSerializer.Meta):
        fields = SystemErrorLogListSerializer.Meta.fields + (
            "traceback_text",
            "query_string",
            "request_body",
            "extra",
            "resolution_notes",
        )


_APPOINTMENT_FIELD_NAMES = tuple(f.name for f in Appointment._meta.fields)


class AppointmentSerializer(serializers.ModelSerializer):
    """Staff/doctor PATCH: optional waive_late_cancel_fee when cancelling a massage inside the 24h window."""

    waive_late_cancel_fee = serializers.BooleanField(required=False, write_only=True, default=False)
    reason_for_visit = serializers.SerializerMethodField()

    class Meta:
        model = Appointment
        fields = _APPOINTMENT_FIELD_NAMES + ("waive_late_cancel_fee", "reason_for_visit")
        read_only_fields = (
            "day_before_reminder_sms_at",
            "day_before_reminder_email_at",
            "same_day_reminder_sms_at",
            "same_day_reminder_email_at",
            "google_calendar_event_id",
        )

    def get_reason_for_visit(self, obj):
        return _appointment_reason_for_visit(obj)


def _appointment_reason_for_visit(obj) -> str:
    """Patient-entered reason at online booking (stored on linked visit, if any)."""
    try:
        visit = obj.visit
    except Visit.DoesNotExist:
        return ""
    return (visit.reason_for_visit or "").strip()


class AppointmentListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for admin schedule/dashboard with readable names."""

    patient_name = serializers.SerializerMethodField()
    provider_name = serializers.SerializerMethodField()
    service_name = serializers.SerializerMethodField()
    service_type = serializers.SerializerMethodField()
    start_time_display = serializers.SerializerMethodField()
    end_time_display = serializers.SerializerMethodField()
    reason_for_visit = serializers.SerializerMethodField()
    patient_date_of_birth = serializers.SerializerMethodField()
    patient_payment_profile = serializers.SerializerMethodField()
    invoice_kind = serializers.SerializerMethodField()
    display_status = serializers.SerializerMethodField()
    auto_no_show_processed_at = serializers.DateTimeField(read_only=True)
    auto_no_show_exempt = serializers.BooleanField(required=False)
    auto_no_show_countdown = serializers.SerializerMethodField()
    invoice_id = serializers.SerializerMethodField()
    invoice_total = serializers.SerializerMethodField()
    amount_paid = serializers.SerializerMethodField()
    amount_due = serializers.SerializerMethodField()

    class Meta:
        model = Appointment
        fields = (
            "id",
            "patient",
            "patient_name",
            "patient_date_of_birth",
            "patient_payment_profile",
            "provider",
            "provider_name",
            "booked_service",
            "service_name",
            "service_type",
            "appointment_date",
            "start_time",
            "end_time",
            "start_time_display",
            "end_time_display",
            "status",
            "display_status",
            "invoice_kind",
            "invoice_id",
            "invoice_total",
            "amount_paid",
            "amount_due",
            "auto_no_show_processed_at",
            "auto_no_show_exempt",
            "auto_no_show_countdown",
            "reason_for_visit",
        )

    def get_patient_name(self, obj):
        return f"{obj.patient.first_name} {obj.patient.last_name}"

    def get_provider_name(self, obj):
        return str(obj.provider)

    def get_service_name(self, obj):
        return obj.booked_service.name if obj.booked_service else ""

    def get_service_type(self, obj):
        return obj.booked_service.service_type if obj.booked_service else ""

    def get_start_time_display(self, obj):
        return obj.start_time.strftime("%I:%M %p")

    def get_end_time_display(self, obj):
        return obj.end_time.strftime("%I:%M %p")

    def get_reason_for_visit(self, obj):
        return _appointment_reason_for_visit(obj)

    def get_patient_date_of_birth(self, obj):
        dob = obj.patient.date_of_birth
        return str(dob) if dob else None

    def get_patient_payment_profile(self, obj):
        return (obj.patient.payment_profile or "").strip()

    def get_invoice_kind(self, obj):
        try:
            return obj.invoice.kind
        except Invoice.DoesNotExist:
            return None

    def get_display_status(self, obj):
        from apps.clinic.appointment_display import appointment_ui_status

        return appointment_ui_status(obj)

    def get_auto_no_show_countdown(self, obj):
        from apps.clinic.auto_no_show import auto_no_show_countdown_for_appointment

        return auto_no_show_countdown_for_appointment(obj)

    def _collectible_invoice(self, obj):
        from apps.clinic.invoice_collection import open_invoice_for_appointment_payment

        return open_invoice_for_appointment_payment(obj)

    def get_invoice_id(self, obj):
        inv = self._collectible_invoice(obj)
        return inv.id if inv else None

    def get_invoice_total(self, obj):
        inv = self._collectible_invoice(obj)
        return str(inv.total_amount) if inv else None

    def get_amount_paid(self, obj):
        from apps.clinic.invoice_collection import invoice_payment_summary

        inv = self._collectible_invoice(obj)
        if not inv:
            return None
        return invoice_payment_summary(inv).get("amount_paid")

    def get_amount_due(self, obj):
        from apps.clinic.invoice_collection import invoice_payment_summary

        inv = self._collectible_invoice(obj)
        if not inv:
            return None
        return invoice_payment_summary(inv).get("amount_due")


class PublicBookingSerializer(serializers.Serializer):
    first_name = serializers.CharField(max_length=100)
    last_name = serializers.CharField(max_length=100)
    phone = serializers.CharField(max_length=20)
    email = serializers.EmailField(required=False, allow_blank=True)
    # Web: True when the SMS consent checkbox is checked. Voice booking sends True (phone channel).
    sms_consent = serializers.BooleanField(required=False, default=True)
    provider_id = serializers.IntegerField(required=False, allow_null=True)
    provider_name = serializers.CharField(max_length=200, required=False, allow_blank=True)
    service_id = serializers.IntegerField(required=False, allow_null=True)
    service_name = serializers.CharField(max_length=200, required=False, allow_blank=True)
    service_duration_minutes = serializers.IntegerField(min_value=5, max_value=240)
    service_price = serializers.DecimalField(max_digits=10, decimal_places=2)
    appointment_date = serializers.DateField()
    start_time = serializers.TimeField(input_formats=["%I:%M %p", "%H:%M"])
    reason_for_visit = serializers.CharField(required=False, allow_blank=True, max_length=2000, default="")

    def validate(self, attrs):
        valid, msg = validate_phone(attrs.get("phone", ""))
        if not valid:
            raise serializers.ValidationError({"phone": msg})
        if attrs.get("service_id"):
            svc = Service.objects.filter(
                pk=attrs["service_id"],
                is_active=True,
                show_in_public_booking=True,
            ).first()
            if not svc:
                raise serializers.ValidationError(
                    {"service_id": "Invalid or inactive service for online booking."}
                )
            # Always use catalog duration/price for slot checks and appointment end time (not client body).
            attrs["service_duration_minutes"] = int(svc.duration_minutes)
            attrs["service_price"] = svc.price
            attrs["service_name"] = svc.label_for_public_booking()
        if attrs.get("provider_id"):
            if not Provider.objects.filter(pk=attrs["provider_id"], active=True).exists():
                raise serializers.ValidationError({"provider_id": "Invalid or inactive provider."})
        return attrs


class PublicRescheduleSerializer(serializers.Serializer):
    """Patient self-service reschedule (verified by phone on file)."""

    phone = serializers.CharField(max_length=20)
    appointment_id = serializers.IntegerField(min_value=1)
    appointment_date = serializers.DateField()
    start_time = serializers.TimeField(input_formats=["%I:%M %p", "%H:%M", "%H:%M:%S"])
    sms_consent = serializers.BooleanField(required=False, default=True)

    def validate(self, attrs):
        valid, msg = validate_phone(attrs.get("phone", ""))
        if not valid:
            raise serializers.ValidationError({"phone": msg})
        return attrs


class PublicCancelSerializer(serializers.Serializer):
    """Patient self-service cancel before visit start (policy fees apply for late massage cancel)."""

    phone = serializers.CharField(max_length=20)
    appointment_id = serializers.IntegerField(min_value=1)

    def validate(self, attrs):
        valid, msg = validate_phone(attrs.get("phone", ""))
        if not valid:
            raise serializers.ValidationError({"phone": msg})
        return attrs


class RecurringBookingPreviewSerializer(serializers.Serializer):
    """Preview recurring dates/slots without creating appointments."""

    service_id = serializers.IntegerField()
    provider_id = serializers.IntegerField()
    appointment_date = serializers.DateField()
    start_time = serializers.TimeField(input_formats=["%I:%M %p", "%H:%M", "%H:%M:%S"])
    recurrence = serializers.ChoiceField(choices=["weekly", "biweekly", "monthly"])
    occurrence_count = serializers.IntegerField(min_value=2, max_value=12, default=4)
    phone = serializers.CharField(max_length=20, required=False, allow_blank=True, default="")

    def validate(self, attrs):
        phone = (attrs.get("phone") or "").strip()
        if phone:
            valid, msg = validate_phone(phone)
            if not valid:
                raise serializers.ValidationError({"phone": msg})
        svc = Service.objects.filter(
            pk=attrs["service_id"],
            is_active=True,
            show_in_public_booking=True,
        ).first()
        if not svc:
            raise serializers.ValidationError(
                {"service_id": "Invalid or inactive service for online booking."}
            )
        if not Provider.objects.filter(pk=attrs["provider_id"], active=True).exists():
            raise serializers.ValidationError({"provider_id": "Invalid or inactive provider."})
        return attrs


class RecurringBookingSerializer(PublicBookingSerializer):
    """Book a recurring series (same service, provider, and time each visit)."""

    recurrence = serializers.ChoiceField(choices=["weekly", "biweekly", "monthly"])
    occurrence_count = serializers.IntegerField(min_value=2, max_value=12, default=4)


class DeskRecurringBookingPreviewSerializer(serializers.Serializer):
    """Staff desk: preview recurring visits for an existing patient."""

    patient_id = serializers.IntegerField(min_value=1)
    service_id = serializers.IntegerField()
    provider_id = serializers.IntegerField()
    appointment_date = serializers.DateField()
    start_time = serializers.TimeField(input_formats=["%I:%M %p", "%H:%M", "%H:%M:%S"])
    recurrence = serializers.ChoiceField(choices=["weekly", "biweekly", "monthly"])
    occurrence_count = serializers.IntegerField(min_value=2, max_value=12, default=4)

    def validate(self, attrs):
        svc = Service.objects.filter(
            pk=attrs["service_id"],
            is_active=True,
            show_in_public_booking=True,
        ).first()
        if not svc:
            raise serializers.ValidationError(
                {"service_id": "Invalid or inactive service for booking."}
            )
        if not Provider.objects.filter(pk=attrs["provider_id"], active=True).exists():
            raise serializers.ValidationError({"provider_id": "Invalid or inactive provider."})
        if not Patient.objects.filter(pk=attrs["patient_id"]).exists():
            raise serializers.ValidationError({"patient_id": "Patient not found."})
        return attrs


class DeskRecurringBookingSerializer(DeskRecurringBookingPreviewSerializer):
    """Staff desk: book a recurring series for an existing patient."""


class VisitRenderedServiceSerializer(serializers.ModelSerializer):
    class Meta:
        model = VisitRenderedService
        # `created_at` / `updated_at` are not used by the billing or visit-completion UI.
        fields = (
            "id",
            "visit",
            "service",
            "quantity",
            "unit_price",
            "total_price",
            "charges_patient",
        )


class VisitSerializer(serializers.ModelSerializer):
    rendered_services = VisitRenderedServiceSerializer(many=True, read_only=True)

    class Meta:
        model = Visit
        # `created_at` / `updated_at` are not needed by any visit list or detail view in the frontend.
        fields = (
            "id",
            "appointment",
            "patient",
            "provider",
            "status",
            "reason_for_visit",
            "doctor_notes",
            "diagnosis",
            "completed_at",
            "rendered_services",
        )


class VisitCompleteSerializer(serializers.Serializer):
    doctor_notes = serializers.CharField(required=False, allow_blank=True)
    rendered_services = serializers.ListField(child=serializers.DictField(), allow_empty=False)


class InvoiceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Invoice
        # `created_at` / `updated_at` are not used by the billing UI or any invoice detail view.
        fields = (
            "id",
            "patient",
            "appointment",
            "visit",
            "kind",
            "invoice_number",
            "subtotal",
            "tax",
            "discount",
            "credit_applied_total",
            "professional_discount_reason",
            "total_amount",
            "status",
            "issued_at",
            "paid_at",
        )


class PaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Payment
        # `created_at` / `updated_at` are not used by any payment view in the frontend.
        fields = (
            "id",
            "invoice",
            "patient",
            "amount",
            "payment_method",
            "payment_reference",
            "status",
            "paid_at",
        )


class PaymentCompleteSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=10, decimal_places=2)
    payment_method = serializers.ChoiceField(choices=Payment.Method.choices)
    payment_reference = serializers.CharField(required=False, allow_blank=True)

    def save(self, *, invoice: Invoice):
        from django.db import transaction

        from apps.clinic.invoice_collection import invoice_amount_due, set_appointment_status_after_invoice_paid

        amount = Decimal(self.validated_data["amount"]).quantize(Decimal("0.01"))
        if amount <= Decimal("0"):
            raise serializers.ValidationError({"amount": "Amount must be greater than zero."})

        with transaction.atomic():
            inv = Invoice.objects.select_for_update().select_related("appointment", "patient").get(pk=invoice.pk)
            if inv.status not in (Invoice.Status.ISSUED, Invoice.Status.OVERDUE, Invoice.Status.DRAFT):
                raise serializers.ValidationError("This invoice cannot be paid in its current state.")

            due_before = invoice_amount_due(inv)
            if due_before <= Decimal("0"):
                raise serializers.ValidationError("This invoice is already paid in full.")

            if amount > due_before:
                raise serializers.ValidationError(
                    {
                        "amount": (
                            f"Amount cannot exceed ${due_before} still due on this invoice "
                            f"(invoice total ${inv.total_amount})."
                        ),
                    }
                )

            payment = Payment.objects.create(
                invoice=inv,
                patient=inv.patient,
                amount=amount,
                payment_method=self.validated_data["payment_method"],
                payment_reference=self.validated_data.get("payment_reference", ""),
                status=Payment.Status.SUCCESSFUL,
                paid_at=timezone.now(),
            )

            due_after = invoice_amount_due(inv)
            if due_after <= Decimal("0"):
                inv.status = Invoice.Status.PAID
                inv.paid_at = timezone.now()
                inv.save(update_fields=["status", "paid_at", "updated_at"])
                set_appointment_status_after_invoice_paid(inv)
            else:
                inv.save(update_fields=["updated_at"])

            self._result_invoice = inv
            self._remaining_due = due_after

            # Mirror cash into Square after commit so Dashboard/reports match — never blocks local pay.
            if payment.payment_method == Payment.Method.CASH:
                cash_payment_id = payment.id

                def _mirror_cash() -> None:
                    try:
                        from apps.clinic.square_payment import record_local_cash_payment_in_square

                        record_local_cash_payment_in_square(cash_payment_id)
                    except Exception:
                        logger.exception(
                            "Square cash mirror on_commit failed for payment %s", cash_payment_id
                        )

                transaction.on_commit(_mirror_cash)

            return payment

    @property
    def remaining_due(self) -> Decimal:
        return getattr(self, "_remaining_due", Decimal("0"))

    @property
    def result_invoice(self) -> Invoice:
        return getattr(self, "_result_invoice")


class DoctorRenderedLineSerializer(serializers.Serializer):
    service_id = serializers.IntegerField()
    quantity = serializers.IntegerField(min_value=1, default=1)
    unit_price = serializers.DecimalField(max_digits=10, decimal_places=2, required=False, allow_null=True)

    def validate_service_id(self, value):
        if not Service.objects.filter(pk=value, is_active=True).exists():
            raise serializers.ValidationError("Invalid or inactive service.")
        return value


class DoctorCompleteVisitSerializer(serializers.Serializer):
    """Doctor finishes visit: notes, diagnosis, and billable service lines (CPT / fees)."""

    doctor_notes = serializers.CharField(required=False, allow_blank=True, default="")
    diagnosis = serializers.CharField(required=False, allow_blank=True, default="")
    diagnosis_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        required=False,
        allow_empty=True,
    )
    rendered_services = serializers.ListField(child=serializers.DictField(), allow_empty=False)
    professional_discount = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        required=False,
        min_value=Decimal("0"),
        default=Decimal("0"),
    )
    professional_discount_reason = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=1000,
        default="",
    )
    charge_saved_card_if_present = serializers.BooleanField(default=True)

    def validate_diagnosis_ids(self, value):
        if not value:
            return value
        found = set(
            DiagnosisCode.objects.filter(pk__in=value, is_active=True).values_list("pk", flat=True)
        )
        missing = sorted(set(value) - found)
        if missing:
            raise serializers.ValidationError(
                f"Invalid or inactive diagnosis id(s): {', '.join(str(i) for i in missing)}."
            )
        return value

    def validate_rendered_services(self, value):
        if not value:
            raise serializers.ValidationError("Add at least one service for this visit.")
        validated_lines = []
        for raw in value:
            line = DoctorRenderedLineSerializer(data=raw)
            line.is_valid(raise_exception=True)
            validated_lines.append(line.validated_data)
        return validated_lines


class SaveSquareCardSerializer(serializers.Serializer):
    """Web Payments SDK token (source_id) after card.tokenize()."""

    phone = serializers.CharField(max_length=20)
    source_id = serializers.CharField(max_length=255)
    verification_token = serializers.CharField(required=False, allow_blank=True, max_length=512)
    # Required only when no patient exists yet for this phone (new guest on public booking).
    first_name = serializers.CharField(max_length=100, required=False, allow_blank=True)
    last_name = serializers.CharField(max_length=100, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)

    def validate(self, attrs):
        valid, msg = validate_phone(attrs.get("phone", ""))
        if not valid:
            raise serializers.ValidationError({"phone": msg})
        return attrs


class StaffSavePatientCardSerializer(serializers.Serializer):
    """Staff/doctor: save Square card on file for an existing patient (Web Payments token)."""

    source_id = serializers.CharField(max_length=255)
    verification_token = serializers.CharField(required=False, allow_blank=True, max_length=512)
    set_as_default = serializers.BooleanField(required=False, default=True)


class TerminalCheckoutSerializer(serializers.Serializer):
    invoice_id = serializers.IntegerField(min_value=1)
    include_pending_fees = serializers.BooleanField(required=False, default=False)


class TerminalCheckoutStatusSerializer(serializers.Serializer):
    checkout_id = serializers.CharField(max_length=255)


class TerminalCheckoutTestSerializer(serializers.Serializer):
    """USD amount for admin Terminal test (no invoice)."""

    amount = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        min_value=Decimal("1.00"),
        max_value=Decimal("9999.99"),
    )


class PatientIntakeUpdateSerializer(serializers.Serializer):
    first_name = serializers.CharField(required=False, allow_blank=False, max_length=100, trim_whitespace=True)
    last_name = serializers.CharField(required=False, allow_blank=False, max_length=100, trim_whitespace=True)
    phone = serializers.CharField(required=False, allow_blank=False, max_length=30)
    email = serializers.EmailField(required=False, allow_blank=True, max_length=254)
    address_line1 = serializers.CharField(required=False, allow_blank=True, max_length=200)
    address_line2 = serializers.CharField(required=False, allow_blank=True, max_length=200)
    city_state_zip = serializers.CharField(required=False, allow_blank=True, max_length=200)
    emergency_contact_name = serializers.CharField(required=False, allow_blank=True, max_length=200)
    emergency_contact_phone = serializers.CharField(required=False, allow_blank=True, max_length=30)
    date_of_birth = serializers.DateField(required=False, allow_null=True)
    date_established = serializers.DateField(required=False, allow_null=True)
    marital_status = serializers.CharField(required=False, allow_blank=True, max_length=1)
    sex = serializers.ChoiceField(choices=["", "M", "F"], required=False, allow_blank=True)
    insurance_company_id = serializers.IntegerField(required=False, allow_null=True)
    insurance_payer_name = serializers.CharField(required=False, allow_blank=True, max_length=200)
    insurance_member_id = serializers.CharField(required=False, allow_blank=True, max_length=64)
    insurance_group_number = serializers.CharField(required=False, allow_blank=True, max_length=64)
    insurance_plan_type = serializers.ChoiceField(
        choices=["", "medicare", "medicaid", "tricare", "champva", "group", "feca", "other"],
        required=False,
        allow_blank=True,
    )
    insurance_relationship = serializers.ChoiceField(
        choices=["", "self", "spouse", "child", "other"],
        required=False,
        allow_blank=True,
    )
    insured_name = serializers.CharField(required=False, allow_blank=True, max_length=200)

    def validate_phone(self, value):
        if value is None:
            return value
        valid, result = validate_phone(value or "")
        if not valid:
            raise serializers.ValidationError(result)
        return result

    def validate_marital_status(self, value):
        v = (value or "").strip().upper()
        if v in ("", "Y", "N"):
            return v
        raise serializers.ValidationError("Use Y (married), N (not married), or leave blank.")
    # Only owner_admin/staff may persist this (see AdminViewSet.patient_intake); doctors’ PATCH ignores it.
    online_chiro_intake_waived = serializers.BooleanField(required=False)
    sms_consent = serializers.BooleanField(required=False)
    notify_booking = serializers.ChoiceField(
        choices=["sms", "email", "both", "none"], required=False
    )
    notify_reminders = serializers.ChoiceField(
        choices=["sms", "email", "both", "none"], required=False
    )
    notify_bills = serializers.ChoiceField(choices=["sms", "email", "both", "none"], required=False)
    payment_profile = serializers.ChoiceField(
        choices=["", "insurance", "cash"],
        required=False,
        allow_blank=True,
    )


class ClinicProfileUpdateSerializer(serializers.Serializer):
    """Partial update for admin Settings (owner/staff only)."""

    timezone = serializers.CharField(max_length=64, required=False)
    clinic_name = serializers.CharField(max_length=200, required=False)
    address_line1 = serializers.CharField(max_length=200, required=False, allow_blank=True)
    city_state_zip = serializers.CharField(max_length=200, required=False, allow_blank=True)
    phone = serializers.CharField(max_length=40, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    employer_tax_id = serializers.CharField(max_length=32, required=False, allow_blank=True)
    provider_billing_id = serializers.CharField(max_length=32, required=False, allow_blank=True)
    pos_default = serializers.CharField(max_length=10, required=False, allow_blank=True)
    no_show_fee = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        min_value=Decimal("0"),
        required=False,
    )
    auto_no_show_enabled = serializers.BooleanField(required=False)
    auto_no_show_grace_minutes = serializers.IntegerField(
        min_value=15,
        max_value=240,
        required=False,
    )
    business_hours = serializers.ListField(
        child=serializers.DictField(child=serializers.CharField(allow_blank=True)),
        required=False,
    )

    def validate_timezone(self, value):
        from apps.clinic.timezone_utils import is_valid_iana_timezone

        tz_name = (value or "").strip() or "America/Detroit"
        if not is_valid_iana_timezone(tz_name):
            raise serializers.ValidationError("Please select a valid timezone")
        return tz_name

    def validate_business_hours(self, value):
        for row in value:
            if "day" not in row or "hours" not in row:
                raise serializers.ValidationError("Each business_hours row must include 'day' and 'hours'.")
        return value


class PatientCreditTopUpSerializer(serializers.Serializer):
    patient_id = serializers.IntegerField(min_value=1)
    amount = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0.01"))
    note = serializers.CharField(required=False, allow_blank=True, max_length=300, default="")


class InvoiceApplyCreditSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=Decimal("0.01"), required=False)


class PatientCreditTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = PatientCreditTransaction
        # `updated_at` is not used by the credit ledger view in the frontend.
        fields = (
            "id",
            "patient",
            "invoice",
            "kind",
            "amount",
            "balance_after",
            "note",
            "created_by",
            "created_at",
        )


def complete_visit_with_services(visit: Visit, payload: dict) -> Invoice:
    visit.doctor_notes = payload.get("doctor_notes", "")
    update_fields = ["doctor_notes", "status", "completed_at", "updated_at"]
    diag_fields = update_visit_diagnosis_fields(visit, payload)
    for f in diag_fields:
        if f not in update_fields:
            update_fields.insert(1, f)
    visit.status = Visit.Status.COMPLETED
    visit.completed_at = timezone.now()
    visit.save(update_fields=update_fields)

    subtotal = Decimal("0")
    visit.rendered_services.all().delete()
    for line in payload["rendered_services"]:
        service = Service.objects.get(pk=line["service_id"])
        qty = Decimal(str(line.get("quantity", 1)))
        unit_price = Decimal(str(line.get("unit_price", service.price)))
        total = qty * unit_price
        charges_patient = service.charges_patient
        if charges_patient:
            subtotal += total
        VisitRenderedService.objects.create(
            visit=visit,
            service=service,
            quantity=int(qty),
            unit_price=unit_price,
            total_price=total,
            charges_patient=charges_patient,
        )

    discount = Decimal(str(payload.get("professional_discount", "0") or "0"))
    discount_reason = (payload.get("professional_discount_reason", "") or "").strip()
    if discount < 0:
        discount = Decimal("0")
    if discount > subtotal:
        discount = subtotal
    if discount == Decimal("0"):
        discount_reason = ""
    total_amount = subtotal - discount

    invoice = Invoice.objects.create(
        patient=visit.patient,
        appointment=visit.appointment,
        visit=visit,
        invoice_number=f"INV-{visit.id}-{int(timezone.now().timestamp())}",
        subtotal=subtotal,
        tax=Decimal("0"),
        discount=discount,
        professional_discount_reason=discount_reason,
        total_amount=total_amount,
        status=Invoice.Status.ISSUED,
    )

    appointment = visit.appointment
    appointment.status = Appointment.Status.AWAITING_PAYMENT
    appointment.save(update_fields=["status", "updated_at"])
    return invoice


def _ensure_visit_completed_for_billing_revision(visit: Visit) -> None:
    if visit.status == Visit.Status.COMPLETED:
        return
    if visit.status in (Visit.Status.IN_PROGRESS, Visit.Status.OPEN):
        visit.status = Visit.Status.COMPLETED
        visit.completed_at = visit.completed_at or timezone.now()
        visit.save(update_fields=["status", "completed_at", "updated_at"])
        return
    raise ValueError("Visit must be completed before revising billing.")


def _apply_visit_billing_revision(visit: Visit, invoice: Invoice, payload: dict) -> Invoice:
    """Persist chart notes, diagnoses, rendered lines, and invoice totals."""
    with transaction.atomic():
        visit.doctor_notes = payload.get("doctor_notes", "")
        update_fields = ["doctor_notes", "updated_at"]
        for f in update_visit_diagnosis_fields(visit, payload):
            if f not in update_fields:
                update_fields.insert(1, f)
        visit.save(update_fields=update_fields)

        subtotal = Decimal("0")
        visit.rendered_services.all().delete()
        for line in payload["rendered_services"]:
            service = Service.objects.get(pk=line["service_id"])
            qty = Decimal(str(line.get("quantity", 1)))
            unit_price = Decimal(str(line.get("unit_price", service.price)))
            total = qty * unit_price
            charges_patient = service.charges_patient
            if charges_patient:
                subtotal += total
            VisitRenderedService.objects.create(
                visit=visit,
                service=service,
                quantity=int(qty),
                unit_price=unit_price,
                total_price=total,
                charges_patient=charges_patient,
            )

        discount = Decimal(str(payload.get("professional_discount", "0") or "0"))
        discount_reason = (payload.get("professional_discount_reason", "") or "").strip()
        if discount < 0:
            discount = Decimal("0")
        if discount > subtotal:
            discount = subtotal
        if discount == Decimal("0"):
            discount_reason = ""
        total_amount = subtotal - discount

        invoice.subtotal = subtotal
        invoice.tax = Decimal("0")
        invoice.discount = discount
        invoice.professional_discount_reason = discount_reason
        invoice.total_amount = total_amount
        invoice.save(
            update_fields=[
                "subtotal",
                "tax",
                "discount",
                "professional_discount_reason",
                "total_amount",
                "updated_at",
            ]
        )

    return invoice


def _reconcile_invoice_status_after_admin_revision(invoice: Invoice) -> None:
    """After admin changes totals, reopen invoice / appointment when money is still owed."""
    from apps.clinic.invoice_collection import invoice_amount_due, set_appointment_status_after_invoice_paid

    due = invoice_amount_due(invoice)
    appt = invoice.appointment
    if due <= Decimal("0"):
        if invoice.status != Invoice.Status.PAID:
            invoice.status = Invoice.Status.PAID
            invoice.paid_at = invoice.paid_at or timezone.now()
            invoice.save(update_fields=["status", "paid_at", "updated_at"])
        set_appointment_status_after_invoice_paid(invoice)
        return

    changed: list[str] = []
    if invoice.status == Invoice.Status.PAID:
        invoice.status = Invoice.Status.ISSUED
        changed.extend(["status"])
    if changed:
        changed.append("updated_at")
        invoice.save(update_fields=changed)
    if appt.status == Appointment.Status.COMPLETED:
        appt.status = Appointment.Status.AWAITING_PAYMENT
        appt.save(update_fields=["status", "updated_at"])


def revise_unpaid_visit_billing(visit: Visit, payload: dict) -> Invoice:
    """Update visit chart lines and invoice totals while appointment is awaiting payment (invoice not paid)."""
    appt = visit.appointment
    if appt.status != Appointment.Status.AWAITING_PAYMENT:
        raise ValueError("You can only edit billing while the visit is awaiting payment.")
    _ensure_visit_completed_for_billing_revision(visit)

    try:
        invoice = Invoice.objects.get(visit=visit)
    except Invoice.DoesNotExist as exc:
        raise ValueError("No invoice found for this visit.") from exc

    if invoice.status == Invoice.Status.PAID:
        raise ValueError("This invoice is already paid — billing cannot be changed here.")
    if invoice.status not in (Invoice.Status.ISSUED, Invoice.Status.OVERDUE, Invoice.Status.DRAFT):
        raise ValueError("This invoice cannot be revised in its current state.")

    return _apply_visit_billing_revision(visit, invoice, payload)


def revise_visit_billing_admin(visit: Visit, payload: dict) -> Invoice:
    """Owner/staff: revise visit invoice after completion (including correcting paid invoices)."""
    appt = visit.appointment
    if appt.status not in (Appointment.Status.AWAITING_PAYMENT, Appointment.Status.COMPLETED):
        raise ValueError("Billing can only be edited for completed visits or visits awaiting payment.")
    _ensure_visit_completed_for_billing_revision(visit)

    try:
        invoice = Invoice.objects.get(visit=visit)
    except Invoice.DoesNotExist as exc:
        raise ValueError("No invoice found for this visit.") from exc

    if invoice.kind != Invoice.Kind.VISIT:
        raise ValueError("Only normal visit invoices can be revised here.")
    if invoice.status == Invoice.Status.VOID:
        raise ValueError("This invoice is void and cannot be changed.")
    if invoice.status not in (
        Invoice.Status.ISSUED,
        Invoice.Status.OVERDUE,
        Invoice.Status.DRAFT,
        Invoice.Status.PAID,
    ):
        raise ValueError("This invoice cannot be revised in its current state.")

    invoice = _apply_visit_billing_revision(visit, invoice, payload)
    _reconcile_invoice_status_after_admin_revision(invoice)
    invoice.refresh_from_db()
    return invoice
