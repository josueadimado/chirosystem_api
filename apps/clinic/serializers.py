from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from rest_framework import serializers

from .patient_phone import duplicate_patient_message, find_duplicate_patient
from .utils import validate_phone
from .models import (
    Appointment,
    DiagnosisCode,
    Invoice,
    Patient,
    PatientCreditTransaction,
    Payment,
    Provider,
    ProviderUnavailability,
    Service,
    StaffNotification,
    Visit,
    VisitRenderedService,
    VoiceCallLog,
)
from .visit_diagnosis import update_visit_diagnosis_fields

User = get_user_model()


class PatientSerializer(serializers.ModelSerializer):
    class Meta:
        model = Patient
        fields = "__all__"

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
        fields = "__all__"


class DiagnosisCodeSerializer(serializers.ModelSerializer):
    class Meta:
        model = DiagnosisCode
        fields = "__all__"


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
    """Update persistent per-appointment chart / handoff notes (doctor on own appts, admin/staff any)."""

    appointment_id = serializers.IntegerField(min_value=1)
    clinical_handoff_notes = serializers.CharField(allow_blank=True, max_length=20000)


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

    class Meta:
        model = Appointment
        fields = (
            "id",
            "patient",
            "patient_name",
            "patient_date_of_birth",
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


class PublicBookingSerializer(serializers.Serializer):
    first_name = serializers.CharField(max_length=100)
    last_name = serializers.CharField(max_length=100)
    phone = serializers.CharField(max_length=20)
    email = serializers.EmailField(required=False, allow_blank=True)
    # Web: True when the SMS consent checkbox is checked. Voice booking sends True (phone channel).
    sms_consent = serializers.BooleanField(required=False, default=False)
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
            if not Service.objects.filter(pk=attrs["service_id"], is_active=True).exists():
                raise serializers.ValidationError({"service_id": "Invalid or inactive service."})
        if attrs.get("provider_id"):
            if not Provider.objects.filter(pk=attrs["provider_id"], active=True).exists():
                raise serializers.ValidationError({"provider_id": "Invalid or inactive provider."})
        return attrs


class PublicRescheduleSerializer(serializers.Serializer):
    """Patient self-service reschedule (verified by phone on file)."""

    phone = serializers.CharField(max_length=20)
    appointment_id = serializers.IntegerField(min_value=1)
    appointment_date = serializers.DateField()
    start_time = serializers.TimeField(input_formats=["%I:%M %p", "%H:%M"])
    sms_consent = serializers.BooleanField(required=False, default=False)

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


class VisitRenderedServiceSerializer(serializers.ModelSerializer):
    class Meta:
        model = VisitRenderedService
        fields = "__all__"


class VisitSerializer(serializers.ModelSerializer):
    rendered_services = VisitRenderedServiceSerializer(many=True, read_only=True)

    class Meta:
        model = Visit
        fields = "__all__"


class VisitCompleteSerializer(serializers.Serializer):
    doctor_notes = serializers.CharField(required=False, allow_blank=True)
    rendered_services = serializers.ListField(child=serializers.DictField(), allow_empty=False)


class InvoiceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Invoice
        fields = "__all__"


class PaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Payment
        fields = "__all__"


class PaymentCompleteSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=10, decimal_places=2)
    payment_method = serializers.ChoiceField(choices=Payment.Method.choices)
    payment_reference = serializers.CharField(required=False, allow_blank=True)

    def save(self, *, invoice: Invoice):
        if invoice.status not in (Invoice.Status.ISSUED, Invoice.Status.OVERDUE, Invoice.Status.DRAFT):
            raise serializers.ValidationError("This invoice cannot be paid in its current state.")
        payment = Payment.objects.create(
            invoice=invoice,
            patient=invoice.patient,
            amount=self.validated_data["amount"],
            payment_method=self.validated_data["payment_method"],
            payment_reference=self.validated_data.get("payment_reference", ""),
            status=Payment.Status.SUCCESSFUL,
            paid_at=timezone.now(),
        )
        invoice.status = Invoice.Status.PAID
        invoice.paid_at = timezone.now()
        invoice.save(update_fields=["status", "paid_at", "updated_at"])

        appointment = invoice.appointment
        if appointment.status != Appointment.Status.COMPLETED:
            appointment.status = Appointment.Status.COMPLETED
            appointment.completed_at = timezone.now()
            appointment.save(update_fields=["status", "completed_at", "updated_at"])
        return payment


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


class TerminalCheckoutSerializer(serializers.Serializer):
    invoice_id = serializers.IntegerField(min_value=1)


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
        fields = "__all__"


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


def revise_unpaid_visit_billing(visit: Visit, payload: dict) -> Invoice:
    """Update visit chart lines and invoice totals while appointment is awaiting payment (invoice not paid)."""
    appt = visit.appointment
    if appt.status != Appointment.Status.AWAITING_PAYMENT:
        raise ValueError("You can only edit billing while the visit is awaiting payment.")
    if visit.status != Visit.Status.COMPLETED:
        # Rare inconsistent rows: invoice exists but visit never flipped to completed — repair and continue.
        if visit.status in (Visit.Status.IN_PROGRESS, Visit.Status.OPEN):
            visit.status = Visit.Status.COMPLETED
            visit.completed_at = visit.completed_at or timezone.now()
            visit.save(update_fields=["status", "completed_at", "updated_at"])
        else:
            raise ValueError("Visit must be completed before revising billing.")

    try:
        invoice = Invoice.objects.get(visit=visit)
    except Invoice.DoesNotExist as exc:
        raise ValueError("No invoice found for this visit.") from exc

    if invoice.status == Invoice.Status.PAID:
        raise ValueError("This invoice is already paid — billing cannot be changed here.")
    if invoice.status not in (Invoice.Status.ISSUED, Invoice.Status.OVERDUE, Invoice.Status.DRAFT):
        raise ValueError("This invoice cannot be revised in its current state.")

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
