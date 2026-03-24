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
    phone = models.CharField(max_length=20, unique=True)
    email = models.EmailField(blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    # Intake / demographics (Relief Chiropractic patient form)
    address_line1 = models.CharField(max_length=200, blank=True)
    address_line2 = models.CharField(max_length=200, blank=True)
    city_state_zip = models.CharField(max_length=200, blank=True, help_text="e.g. St Joseph, MI 49085")
    emergency_contact_name = models.CharField(max_length=200, blank=True)
    emergency_contact_phone = models.CharField(max_length=30, blank=True)
    # Square — full card data never stored; only customer + card on file id and display hints
    square_customer_id = models.CharField(max_length=255, blank=True)
    square_card_id = models.CharField(max_length=255, blank=True)
    card_brand = models.CharField(max_length=20, blank=True)
    card_last4 = models.CharField(max_length=4, blank=True)

    def __str__(self) -> str:
        return f"{self.first_name} {self.last_name}"


class Provider(TimeStampedModel):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    title = models.CharField(max_length=100, blank=True)
    specialty = models.CharField(max_length=100, blank=True)
    active = models.BooleanField(default=True)
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
        help_text="Visit types this doctor appears under on online booking. In-room billing uses the full clinic service list.",
    )
    # SMS (Twilio) alerts: check-in, new bookings, schedule/status changes — same env as patient SMS
    notification_phone = models.CharField(
        max_length=20,
        blank=True,
        default="",
        help_text="Doctor/staff mobile for alerts (E.164 e.g. +15551234567). Leave blank to skip.",
    )

    def __str__(self) -> str:
        return self.user.full_name or self.user.username


class Service(TimeStampedModel):
    class ServiceType(models.TextChoices):
        CHIROPRACTIC = "chiropractic", "Chiropractic"
        MASSAGE = "massage", "Massage"

    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    duration_minutes = models.PositiveIntegerField(default=30)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    # CPT / HCPCS / local code + modifiers (e.g. "97012 GP 59")
    billing_code = models.CharField(max_length=64, blank=True)
    is_active = models.BooleanField(default=True)
    # If False: still billable in the doctor visit UI, but hidden from public online booking.
    show_in_public_booking = models.BooleanField(default=True)
    service_type = models.CharField(
        max_length=20,
        choices=ServiceType.choices,
        default=ServiceType.CHIROPRACTIC,
        help_text="Chiropractic: one doctor assigned by admin, no choice. Massage: patient chooses from assigned providers.",
    )

    def __str__(self) -> str:
        return self.name


class Appointment(TimeStampedModel):
    class Status(models.TextChoices):
        BOOKED = "booked", "Booked"
        CONFIRMED = "confirmed", "Confirmed"
        CHECKED_IN = "checked_in", "Checked In"
        IN_CONSULTATION = "in_consultation", "In Consultation"
        AWAITING_PAYMENT = "awaiting_payment", "Awaiting Payment"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"
        NO_SHOW = "no_show", "No Show"

    patient = models.ForeignKey(Patient, on_delete=models.CASCADE)
    provider = models.ForeignKey(Provider, on_delete=models.CASCADE)
    booked_service = models.ForeignKey(Service, on_delete=models.SET_NULL, null=True)
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
        help_text="Clinical or admin notes for future visits—visible to other doctors on this patient's chart.",
    )
    # Twilio: set when a day-before reminder SMS was sent (cleared if date/time rescheduled)
    sms_reminder_sent_at = models.DateTimeField(null=True, blank=True)
    # Google Calendar event on the provider's connected personal calendar
    google_calendar_event_id = models.CharField(max_length=255, blank=True)


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
    diagnosis = models.TextField(blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)


class VisitRenderedService(TimeStampedModel):
    visit = models.ForeignKey(Visit, on_delete=models.CASCADE, related_name="rendered_services")
    service = models.ForeignKey(Service, on_delete=models.CASCADE)
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    total_price = models.DecimalField(max_digits=10, decimal_places=2)


class Invoice(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ISSUED = "issued", "Issued"
        PAID = "paid", "Paid"
        VOID = "void", "Void"
        OVERDUE = "overdue", "Overdue"

    patient = models.ForeignKey(Patient, on_delete=models.CASCADE)
    appointment = models.OneToOneField(Appointment, on_delete=models.CASCADE)
    visit = models.OneToOneField(Visit, on_delete=models.CASCADE)
    invoice_number = models.CharField(max_length=40, unique=True)
    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    tax = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    discount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ISSUED)
    issued_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)


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

    call_sid = models.CharField(max_length=64, unique=True, db_index=True)
    from_number = models.CharField(max_length=32, blank=True)
    transcript = models.TextField(blank=True)
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
    pos_default = models.CharField(
        max_length=10,
        default="11",
        help_text="Default place-of-service code on printed bill lines.",
    )
    business_hours = models.JSONField(default=list)

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
                "business_hours": list(_DEFAULT_CLINIC_BUSINESS_HOURS),
            },
        )
        if not obj.business_hours:
            obj.business_hours = list(_DEFAULT_CLINIC_BUSINESS_HOURS)
            obj.save(update_fields=["business_hours", "updated_at"])
        return obj
