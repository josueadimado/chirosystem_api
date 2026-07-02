from decimal import Decimal

from django.conf import settings
from django.db import models


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Patient(TimeStampedModel):
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    phone = models.CharField(max_length=20, db_index=True)
    email = models.EmailField(blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    date_established = models.DateField(
        null=True,
        blank=True,
        help_text="Staff override for date established. When blank, the system uses the first non-cancelled appointment date.",
    )
    # Intake / demographics (Relief Chiropractic patient form)
    address_line1 = models.CharField(max_length=200, blank=True)
    address_line2 = models.CharField(max_length=200, blank=True)
    city_state_zip = models.CharField(max_length=200, blank=True, help_text="e.g. St Joseph, MI 49085")
    emergency_contact_name = models.CharField(max_length=200, blank=True)
    emergency_contact_phone = models.CharField(max_length=30, blank=True)
    marital_status = models.CharField(
        max_length=1,
        blank=True,
        choices=[("", "—"), ("Y", "Married"), ("N", "Not married")],
        help_text="Y = married, N = not married (matrimonial situation).",
    )
    # Square — full card data never stored; only customer + card on file id and display hints
    square_customer_id = models.CharField(max_length=255, blank=True)
    square_card_id = models.CharField(max_length=255, blank=True)
    card_brand = models.CharField(max_length=20, blank=True)
    card_last4 = models.CharField(max_length=4, blank=True)
    # SMS (TCPA): on by default; staff/doctor can turn off per patient. Online booking can opt out.
    sms_consent = models.BooleanField(
        default=True,
        help_text="True when the patient may receive SMS appointment reminders. On by default; staff or doctor can turn off.",
    )
    sms_consent_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When SMS consent was last turned on (online booking or staff save).",
    )
    # How to reach this patient for automated messages (staff/doctor can override per patient).
    _notify_channel_help = (
        "sms = text only, email = email only, both = both channels, none = no automated messages."
    )
    notify_booking = models.CharField(
        max_length=10,
        choices=[
            ("sms", "Text (SMS) only"),
            ("email", "Email only"),
            ("both", "Text and email"),
            ("none", "None"),
        ],
        default="sms",
        help_text="Booking / reschedule / cancel confirmations. " + _notify_channel_help,
    )
    notify_reminders = models.CharField(
        max_length=10,
        choices=[
            ("sms", "Text (SMS) only"),
            ("email", "Email only"),
            ("both", "Text and email"),
            ("none", "None"),
        ],
        default="sms",
        help_text="Day-before and same-day appointment reminders. SMS also requires SMS consent. "
        + _notify_channel_help,
    )
    notify_bills = models.CharField(
        max_length=10,
        choices=[
            ("sms", "Text (SMS) only"),
            ("email", "Email only"),
            ("both", "Text and email"),
            ("none", "None"),
        ],
        default="email",
        help_text="Paid receipt / patient bill email from the portal. " + _notify_channel_help,
    )
    # When True, public/voice booking skips "must book intake first" for chiropractic (migrated / established patients).
    online_chiro_intake_waived = models.BooleanField(
        default=False,
        help_text="If checked, this patient may book regular (non-intake) chiropractic online even without a completed "
        "chiropractic visit on file—use for data imports and established patients from before the system.",
    )
    credit_balance = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0"),
        help_text="In-house prepaid credit available to apply to future invoices.",
    )
    # Schedule / desk: insurance vs cash pay (set by staff; shown next to name on calendar).
    class PaymentProfile(models.TextChoices):
        UNSET = "", "Not set"
        INSURANCE = "insurance", "Insurance"
        CASH = "cash", "Cash / self-pay"

    payment_profile = models.CharField(
        max_length=20,
        choices=PaymentProfile.choices,
        blank=True,
        default="",
        help_text="Shown on the schedule: insurance (eye icon) or cash. Set by staff during a visit.",
    )

    def __str__(self) -> str:
        return f"{self.first_name} {self.last_name}"


def _patient_document_upload_path(instance: "PatientDocument", filename: str) -> str:
    return f"patient_documents/{instance.patient_id}/{filename}"


class PatientDocument(TimeStampedModel):
    """A file (image, PDF, etc.) attached to a patient's record by staff or a doctor."""

    DOC_TYPES = [
        ("insurance_card", "Insurance Card"),
        ("x_ray", "X-Ray / Imaging"),
        ("lab_result", "Lab Result"),
        ("referral", "Referral Letter"),
        ("intake_form", "Intake Form"),
        ("other", "Other"),
    ]

    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name="documents")
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    file = models.FileField(upload_to=_patient_document_upload_path)
    original_filename = models.CharField(max_length=255)
    label = models.CharField(
        max_length=200,
        help_text="Short descriptive name shown in the chart (e.g. 'Blue Cross card front').",
    )
    doc_type = models.CharField(max_length=30, choices=DOC_TYPES, default="other")

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            # Document list for a patient ordered by upload date
            models.Index(fields=["patient_id", "created_at"], name="patient_doc_patient_date_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.label} — {self.patient}"


class Provider(TimeStampedModel):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    title = models.CharField(max_length=100, blank=True)
    credential = models.CharField(max_length=100, blank=True, help_text="Displayed on printed patient bills, e.g. DC, PT, LMT.")
    specialty = models.CharField(max_length=100, blank=True)
    active = models.BooleanField(default=True)
    primary_service_type = models.CharField(
        max_length=20,
        choices=[("chiropractic", "Chiropractic"), ("massage", "Massage")],
        default="chiropractic",
        help_text="Default online booking category: chiropractic vs massage visit types.",
    )
    # Personal Google Calendar (OAuth) — each doctor connects their own account
    google_refresh_token = models.TextField(
        blank=True, help_text="OAuth refresh token for personal Google Calendar"
    )
    google_calendar_id = models.CharField(
        max_length=255,
        blank=True,
        default="primary",
        help_text="Calendar id to write events to (default: primary)",
    )
    # Which bookable visit types list this provider on the public booking site (not clinical scope / not the in-room bill).
    services = models.ManyToManyField(
        "Service",
        related_name="providers",
        blank=True,
        help_text="Visit types this doctor appears under on online booking. In-room billing uses active services allowed for their role (see each service's staff visibility).",
    )
    # SMS (Twilio) alerts: check-in, new bookings, schedule/status changes — same env as patient SMS
    notification_phone = models.CharField(
        max_length=20,
        blank=True,
        default="",
        help_text="Doctor/staff mobile for alerts (E.164 e.g. +15551234567). Leave blank to skip.",
    )
    billing_provider_id = models.CharField(
        max_length=32,
        blank=True,
        default="",
        help_text="Optional. Billing provider ID (e.g. NPI) printed on this doctor's patient bills. "
        "When blank, the clinic-wide Provider ID from Settings is used.",
    )

    class Meta:
        verbose_name = "Doctor"
        verbose_name_plural = "Doctors"

    def __str__(self) -> str:
        return self.user.full_name or self.user.username


class Service(TimeStampedModel):
    class ServiceType(models.TextChoices):
        CHIROPRACTIC = "chiropractic", "Chiropractic"
        MASSAGE = "massage", "Massage"

    name = models.CharField(max_length=200)
    public_booking_name = models.CharField(
        max_length=200,
        blank=True,
        default="",
        help_text="Optional. When set, patients see this on the public booking site, voice booking, and in confirmations "
        "instead of Name. Schedules, doctor portal, and billing always use Name.",
    )
    description = models.TextField(blank=True)
    duration_minutes = models.PositiveIntegerField(default=30)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    # CPT / HCPCS / local code + modifiers (e.g. "97012 GP 59")
    billing_code = models.CharField(max_length=64, blank=True)
    is_active = models.BooleanField(default=True)
    # If False: still billable in the doctor visit UI, but hidden from public online booking.
    show_in_public_booking = models.BooleanField(default=True)
    # In-room bill picker: which provider roles may add this line (owner/staff still see all in admin).
    visible_to_chiropractic_staff = models.BooleanField(
        default=True,
        help_text="If True, chiropractic doctors see this service when completing a visit.",
    )
    visible_to_massage_staff = models.BooleanField(
        default=True,
        help_text="If True, massage therapists see this service when completing a visit.",
    )
    service_type = models.CharField(
        max_length=20,
        choices=ServiceType.choices,
        default=ServiceType.CHIROPRACTIC,
        help_text="Chiropractic: one doctor assigned by admin, no choice. Massage: patient chooses from assigned providers.",
    )
    is_new_client_intake = models.BooleanField(
        default=False,
        help_text="If True, online chiropractic booking allows patients returning after a long gap (e.g. 2+ years since last completed chiro visit). "
        "Mark one bookable visit type as new patient / reactivation intake.",
    )
    charges_patient = models.BooleanField(
        default=True,
        help_text="If True (typical visit/product), this line is included in the amount the patient pays. "
        "If False, the service still appears on the printed bill with CPT/description for insurance reimbursement, "
        "but its fee is not added to the invoice total.",
    )

    def visible_for_primary_service_type(self, primary_service_type: str) -> bool:
        """Whether this service may appear on the in-room bill for a provider with the given booking category."""
        if primary_service_type == self.ServiceType.CHIROPRACTIC:
            return self.visible_to_chiropractic_staff
        if primary_service_type == self.ServiceType.MASSAGE:
            return self.visible_to_massage_staff
        return True

    def __str__(self) -> str:
        return self.name

    def label_for_public_booking(self) -> str:
        """Patient-facing service title (web/voice booking, SMS/email). Falls back to name if unset."""
        alt = (self.public_booking_name or "").strip()
        return alt if alt else self.name


class AppointmentSeries(TimeStampedModel):
    """Recurring online booking — multiple appointments share one series."""

    class Recurrence(models.TextChoices):
        WEEKLY = "weekly", "Weekly"
        BIWEEKLY = "biweekly", "Every 2 weeks"
        MONTHLY = "monthly", "Monthly"

    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name="appointment_series")
    provider = models.ForeignKey(Provider, on_delete=models.CASCADE)
    booked_service = models.ForeignKey(Service, on_delete=models.SET_NULL, null=True)
    start_time = models.TimeField(help_text="Shared start time for each occurrence.")
    recurrence = models.CharField(max_length=20, choices=Recurrence.choices)
    first_appointment_date = models.DateField()
    last_appointment_date = models.DateField()
    occurrence_count = models.PositiveSmallIntegerField(default=1)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Series #{self.pk} ({self.recurrence}, {self.occurrence_count} visits)"


class Appointment(TimeStampedModel):
    class Status(models.TextChoices):
        BOOKED = "booked", "Booked"
        CHECKED_IN = "checked_in", "Checked In"
        IN_CONSULTATION = "in_consultation", "In Consultation"
        AWAITING_PAYMENT = "awaiting_payment", "Awaiting Payment"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"
        NO_SHOW = "no_show", "No Show"

    patient = models.ForeignKey(Patient, on_delete=models.CASCADE)
    provider = models.ForeignKey(Provider, on_delete=models.CASCADE)
    booked_service = models.ForeignKey(Service, on_delete=models.SET_NULL, null=True)
    series = models.ForeignKey(
        AppointmentSeries,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="appointments",
    )
    appointment_date = models.DateField()
    start_time = models.TimeField()
    end_time = models.TimeField()
    status = models.CharField(max_length=30, choices=Status.choices, default=Status.BOOKED)
    checked_in_at = models.DateTimeField(null=True, blank=True)
    consultation_started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)
    # Persistent chart / handoff text for staff and the assigned provider (visible on patient history; editable per appointment).
    clinical_handoff_notes = models.TextField(
        blank=True,
        default="",
        help_text=(
            "Staff/doctor reminders for this visit (birthday, preferences, handoff to next provider). "
            "Not the same as consultation SOAP notes on the Visit record."
        ),
    )
    # Filled when each reminder channel was last sent for this appointment (nulled when date/time changes)
    day_before_reminder_sms_at = models.DateTimeField(null=True, blank=True)
    day_before_reminder_email_at = models.DateTimeField(null=True, blank=True)
    same_day_reminder_sms_at = models.DateTimeField(null=True, blank=True)
    same_day_reminder_email_at = models.DateTimeField(null=True, blank=True)
    auto_no_show_processed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Set when the system automatically marked this visit as no-show.",
    )
    auto_no_show_exempt = models.BooleanField(
        default=False,
        help_text="When true, this visit is skipped by automatic no-show (staff can still mark no-show manually).",
    )
    no_show_notice_sms_at = models.DateTimeField(null=True, blank=True)
    no_show_notice_email_at = models.DateTimeField(null=True, blank=True)
    late_checkin_sms_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Set when the patient was texted because they had not checked in after the scheduled start.",
    )
    # Google Calendar event on the provider's connected personal calendar
    google_calendar_event_id = models.CharField(max_length=255, blank=True)

    def clear_reminder_timestamps(self) -> None:
        """Clear all reminder send markers (e.g. after reschedule) so reminders can fire again."""
        self.day_before_reminder_sms_at = None
        self.day_before_reminder_email_at = None
        self.same_day_reminder_sms_at = None
        self.same_day_reminder_email_at = None
        self.late_checkin_sms_at = None

    def __str__(self) -> str:
        return f"{self.appointment_date} {self.start_time} ({self.status})"

    class Meta:
        indexes = [
            # Schedule grid: appointments for a provider on a given date (availability, day view)
            models.Index(fields=["provider_id", "appointment_date"], name="appt_provider_date_idx"),
            # Patient chart / kiosk / my-appointments: all visits for a patient ordered by date
            models.Index(fields=["patient_id", "appointment_date"], name="appt_patient_date_idx"),
            # Dashboard counts and status-filtered list queries (e.g. today's checked-in / completed)
            models.Index(fields=["appointment_date", "status"], name="appt_date_status_idx"),
        ]


class ProviderUnavailability(TimeStampedModel):
    """
    Blocks this provider from *online* booking for a calendar date (whole day or a time window).
    Default is available everywhere; only rows here hide slots on the public booking site.
    """

    provider = models.ForeignKey(Provider, on_delete=models.CASCADE, related_name="unavailability_blocks")
    block_date = models.DateField(db_index=True)
    all_day = models.BooleanField(default=True)
    start_time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)

    class Meta:
        ordering = ["-block_date", "start_time"]
        verbose_name = "Provider online booking block"
        verbose_name_plural = "Provider online booking blocks"
        indexes = [
            # Combined filter: unavailability blocks for a provider within a date range
            models.Index(fields=["provider_id", "block_date"], name="unavail_provider_date_idx"),
        ]

    def __str__(self) -> str:
        if self.all_day:
            return f"{self.provider_id} · {self.block_date} (all day)"
        return f"{self.provider_id} · {self.block_date} {self.start_time}–{self.end_time}"


class Visit(TimeStampedModel):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        IN_PROGRESS = "in_progress", "In Progress"
        COMPLETED = "completed", "Completed"

    appointment = models.OneToOneField(Appointment, on_delete=models.CASCADE)
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE)
    provider = models.ForeignKey(Provider, on_delete=models.CASCADE)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    reason_for_visit = models.TextField(blank=True)
    doctor_notes = models.TextField(blank=True)
    diagnosis = models.TextField(
        blank=True,
        help_text="Formatted diagnosis lines for bills and chart (synced from visit_diagnoses when using the catalog).",
    )
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            # Doctor patient list: completed visits for a provider sorted by date
            models.Index(fields=["provider_id", "status", "completed_at"], name="visit_prov_stat_done_idx"),
            # Patient chart: completed visits for a patient sorted by date
            models.Index(fields=["patient_id", "status", "completed_at"], name="visit_pat_stat_done_idx"),
        ]


class DiagnosisCode(TimeStampedModel):
    """Clinic diagnosis catalog — code + description (admin-maintained, chosen during consultations)."""

    code = models.CharField(max_length=32, unique=True)
    description = models.CharField(max_length=500)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["code"]
        verbose_name = "Diagnosis code"
        verbose_name_plural = "Diagnosis codes"

    def __str__(self) -> str:
        return f"{self.code} — {self.description}"


class VisitDiagnosis(TimeStampedModel):
    """Diagnoses selected for a visit (snapshots code/description for history if catalog entry changes)."""

    visit = models.ForeignKey(Visit, on_delete=models.CASCADE, related_name="visit_diagnoses")
    diagnosis = models.ForeignKey(
        DiagnosisCode,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="visit_rows",
    )
    code = models.CharField(max_length=32)
    description = models.CharField(max_length=500)

    class Meta:
        ordering = ["id"]

    def __str__(self) -> str:
        return f"{self.code} — {self.description}"


class VisitRenderedService(TimeStampedModel):
    visit = models.ForeignKey(Visit, on_delete=models.CASCADE, related_name="rendered_services")
    service = models.ForeignKey(Service, on_delete=models.CASCADE)
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    total_price = models.DecimalField(max_digits=10, decimal_places=2)
    # Snapshot of Service.charges_patient at visit completion (invoice math uses this).
    charges_patient = models.BooleanField(
        default=True,
        help_text="If False, this line is shown on the printed bill for insurance but not included in patient invoice totals.",
    )

    class Meta:
        indexes = [
            # Insurance-only billing filter: rendered services for a visit that don't charge the patient
            models.Index(fields=["visit_id", "charges_patient"], name="rendered_svc_visit_charges_idx"),
        ]


class Invoice(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ISSUED = "issued", "Issued"
        PAID = "paid", "Paid"
        VOID = "void", "Void"
        OVERDUE = "overdue", "Overdue"

    class Kind(models.TextChoices):
        VISIT = "visit", "Visit"
        NO_SHOW_FEE = "no_show_fee", "No-show fee"
        LATE_CANCEL_FEE = "late_cancel_fee", "Late cancellation fee"

    patient = models.ForeignKey(Patient, on_delete=models.CASCADE)
    appointment = models.OneToOneField(Appointment, on_delete=models.CASCADE)
    visit = models.OneToOneField(Visit, on_delete=models.CASCADE)
    kind = models.CharField(
        max_length=20,
        choices=Kind.choices,
        default=Kind.VISIT,
        help_text="Visit = normal clinical invoice; penalty kinds = missed visit or late cancellation per clinic policy.",
    )
    invoice_number = models.CharField(max_length=40, unique=True)
    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    tax = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    discount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    credit_applied_total = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0"),
        help_text="Total in-house patient credit applied to this invoice over time.",
    )
    professional_discount_reason = models.TextField(
        blank=True,
        default="",
        help_text="Optional internal note for why a professional discount was applied. Not shown on patient-facing printed bills.",
    )
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ISSUED)
    issued_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            # Billing list filtering and tab counts (open / overdue / paid), ordered by issued date
            models.Index(fields=["status", "issued_at"], name="invoice_status_issued_idx"),
            # Daily revenue query: paid invoices filtered and summed by paid date
            models.Index(fields=["status", "paid_at"], name="invoice_status_paid_idx"),
            # Penalty invoice filter (no-show fee / late cancellation fee)
            models.Index(fields=["kind", "status"], name="invoice_kind_status_idx"),
        ]


class StaffNotification(TimeStampedModel):
    """In-app alerts for logged-in staff (e.g. doctor sees check-ins and schedule changes)."""

    class Kind(models.TextChoices):
        CHECKIN = "checkin", "Check-in"
        NEW_BOOKING = "new_booking", "New booking"
        SCHEDULE_CHANGE = "schedule_change", "Schedule change"
        REASSIGNED_AWAY = "reassigned_away", "Reassigned away"

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="staff_notifications",
    )
    kind = models.CharField(max_length=30, choices=Kind.choices)
    message = models.TextField()
    appointment = models.ForeignKey(
        "Appointment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="staff_notifications",
    )
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["recipient", "created_at"]),
            models.Index(fields=["recipient", "read_at"]),
        ]


class PatientCreditTransaction(TimeStampedModel):
    class Kind(models.TextChoices):
        TOP_UP = "top_up", "Top up"
        APPLY_TO_INVOICE = "apply_to_invoice", "Applied to invoice"
        ADJUSTMENT = "adjustment", "Manual adjustment"

    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name="credit_transactions")
    invoice = models.ForeignKey(
        Invoice,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="credit_transactions",
    )
    kind = models.CharField(max_length=30, choices=Kind.choices)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    balance_after = models.DecimalField(max_digits=10, decimal_places=2)
    note = models.CharField(max_length=300, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_credit_transactions",
    )

    class Meta:
        indexes = [
            # Credit ledger for a patient ordered by transaction date
            models.Index(fields=["patient_id", "created_at"], name="credit_txn_patient_date_idx"),
        ]


class VoiceCallLog(TimeStampedModel):
    """One row per Twilio CallSid; updated as the voice booking flow progresses."""

    class Outcome(models.TextChoices):
        PROMPTED = "prompted", "Greeting played"
        NO_OPENAI = "no_openai", "OpenAI not configured"
        EMPTY_SPEECH = "empty_speech", "No speech detected"
        OPENAI_FAILED = "openai_failed", "Could not understand (AI)"
        INTENT_INCOMPLETE = "intent_incomplete", "Missing name, service, or time"
        SERIALIZER_REJECTED = "serializer_rejected", "Data did not validate"
        SLOT_OR_RULE_ERROR = "slot_or_rule_error", "Slot taken or not bookable"
        BOOKED = "booked", "Appointment created"
        ABANDONED_RETRIES = "abandoned_retries", "Hung up after retries"
        DISCONNECTED = "disconnected", "Disconnected mid-call"

    call_sid = models.CharField(max_length=64, unique=True, db_index=True)
    from_number = models.CharField(max_length=32, blank=True)
    transcript = models.TextField(blank=True)
    # Full call dialogue: list of {role, text, step?, at} — caller + assistant turns.
    conversation_log = models.JSONField(default=list, blank=True)
    outcome = models.CharField(
        max_length=32, choices=Outcome.choices, default=Outcome.PROMPTED
    )
    detail = models.TextField(blank=True)
    appointment = models.ForeignKey(
        "Appointment",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="voice_call_logs",
    )

    class Meta:
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["created_at"]),
            models.Index(fields=["outcome", "created_at"]),
        ]


class SystemErrorLog(TimeStampedModel):
    """Server-side errors captured for the password-protected admin error tracker."""

    class Level(models.TextChoices):
        ERROR = "error", "Error"
        WARNING = "warning", "Warning"
        CRITICAL = "critical", "Critical"

    class Source(models.TextChoices):
        API = "api", "API"
        MIDDLEWARE = "middleware", "Middleware"
        CELERY = "celery", "Celery"
        CLIENT = "client", "Browser (admin)"
        VOICE_AI = "voice_ai", "Voice AI"

    level = models.CharField(max_length=20, choices=Level.choices, default=Level.ERROR)
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.API)
    message = models.TextField()
    exception_type = models.CharField(max_length=200, blank=True, default="")
    traceback_text = models.TextField(blank=True, default="")
    http_method = models.CharField(max_length=10, blank=True, default="")
    path = models.CharField(max_length=500, blank=True, default="")
    query_string = models.CharField(max_length=1000, blank=True, default="")
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    user_id = models.IntegerField(null=True, blank=True)
    user_role = models.CharField(max_length=30, blank=True, default="")
    user_display = models.CharField(max_length=200, blank=True, default="")
    request_body = models.TextField(blank=True, default="")
    extra = models.JSONField(default=dict, blank=True)
    fingerprint = models.CharField(max_length=64, blank=True, default="", db_index=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by_id = models.IntegerField(null=True, blank=True)
    resolution_notes = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["resolved_at", "created_at"]),
            models.Index(fields=["source", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.source} {self.level}: {self.message[:80]}"


class Payment(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        SUCCESSFUL = "successful", "Successful"
        FAILED = "failed", "Failed"
        REFUNDED = "refunded", "Refunded"

    class Method(models.TextChoices):
        CASH = "cash", "Cash"
        CARD = "card", "Card"
        ONLINE = "online", "Online"
        MANUAL = "manual", "Manual"

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="payments")
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    payment_method = models.CharField(max_length=20, choices=Method.choices, default=Method.CARD)
    payment_reference = models.CharField(max_length=120, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            # Per-invoice payment sum: filter successful payments for a given invoice
            models.Index(fields=["invoice_id", "status"], name="payment_invoice_status_idx"),
            # Dashboard recent payments: filter and order by payment date
            models.Index(fields=["paid_at"], name="payment_paid_at_idx"),
        ]


# Defaults for the single clinic settings row (bills + admin Settings page)
_DEFAULT_CLINIC_BUSINESS_HOURS = [
    {"day": "Monday", "hours": "8:00 AM – 5:00 PM"},
    {"day": "Tuesday", "hours": "8:00 AM – 5:00 PM"},
    {"day": "Wednesday", "hours": "8:00 AM – 5:00 PM"},
    {"day": "Thursday", "hours": "8:00 AM – 5:00 PM"},
    {"day": "Friday", "hours": "8:00 AM – 5:00 PM"},
    {"day": "Saturday", "hours": "Closed"},
    {"day": "Sunday", "hours": "Closed"},
]


class ClinicSettings(TimeStampedModel):
    """Single row (pk=1): clinic header for printed bills and admin Settings."""

    clinic_name = models.CharField(max_length=200, default="Relief Chiropractic PC")
    address_line1 = models.CharField(max_length=200, default="3830 M 139, Suite 119")
    city_state_zip = models.CharField(max_length=200, default="St Joseph, MI 49085")
    phone = models.CharField(max_length=40, default="269-408-0303")
    email = models.EmailField(blank=True, default="")
    employer_tax_id = models.CharField(
        max_length=32,
        blank=True,
        default="",
        help_text="Employer / office tax ID printed on patient bills (e.g. EIN). Optional.",
    )
    provider_billing_id = models.CharField(
        max_length=32,
        blank=True,
        default="",
        help_text="Clinic-wide billing provider ID printed on all patient bills (e.g. NPI). "
        "Used when a doctor has no per-provider ID set.",
    )
    pos_default = models.CharField(
        max_length=10,
        default="11",
        help_text="Default place-of-service code on printed bill lines.",
    )
    business_hours = models.JSONField(default=list)
    timezone = models.CharField(
        max_length=64,
        default="America/Detroit",
        help_text=(
            "IANA timezone for the clinic. "
            "Controls scheduling, AI voice, "
            "and appointment times. "
            "Example: America/Detroit, "
            "America/Chicago, America/New_York"
        ),
    )
    no_show_fee = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("25.00"),
        help_text="Fallback no-show amount (USD) when the booked visit type has no price. "
        "Normally chiropractic and massage no-shows use the booked service price. "
        "Set to 0 to skip only that fallback. Card on file is charged when possible; otherwise the visit may stay in Awaiting payment.",
    )
    auto_no_show_enabled = models.BooleanField(
        default=True,
        help_text="Automatically mark unattended visits as no-show after the grace period.",
    )
    auto_no_show_grace_minutes = models.PositiveSmallIntegerField(
        default=60,
        help_text="Minutes after scheduled start before auto no-show runs.",
    )
    error_tracker_password_hash = models.CharField(
        max_length=128,
        blank=True,
        default="",
        help_text="Hashed password for the owner-only admin error tracker page.",
    )

    class Meta:
        verbose_name_plural = "Clinic settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get_solo(cls):
        obj, _ = cls.objects.get_or_create(
            pk=1,
            defaults={
                "clinic_name": "Relief Chiropractic PC",
                "address_line1": "3830 M 139, Suite 119",
                "city_state_zip": "St Joseph, MI 49085",
                "phone": "269-408-0303",
                "email": "",
                "pos_default": "11",
                "no_show_fee": Decimal("25.00"),
                "business_hours": list(_DEFAULT_CLINIC_BUSINESS_HOURS),
                "timezone": "America/Detroit",
            },
        )
        if not obj.business_hours:
            obj.business_hours = list(_DEFAULT_CLINIC_BUSINESS_HOURS)
            obj.save(update_fields=["business_hours", "updated_at"])
        return obj

    @classmethod
    def get_cached(cls):
        """Return the singleton ClinicSettings row from Redis cache (3-min TTL).

        Use this instead of get_solo() on read paths where a slightly stale
        value is acceptable.  The cache is invalidated automatically whenever
        ClinicSettings.save() fires (via post_save signal in signals.py).
        """
        from apps.clinic.cache_utils import get_clinic_settings_cached
        return get_clinic_settings_cached()
