from datetime import timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.signing import TimestampSigner
from django.db import transaction
from django.db.models import Case, IntegerField, Prefetch, Value, When
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import mixins, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from .models import (
    Appointment,
    ClinicSettings,
    Invoice,
    Patient,
    Payment,
    Provider,
    ProviderUnavailability,
    Service,
    StaffNotification,
    Visit,
    VisitRenderedService,
    VoiceCallLog,
)
from .public_booking_service import create_appointment_from_public_booking
from .utils import format_time_12h, normalize_phone, validate_phone
from .serializers import (
    AppointmentHandoffNotesSerializer,
    AppointmentListSerializer,
    AppointmentSerializer,
    ClinicProfileUpdateSerializer,
    DoctorCompleteVisitSerializer,
    InvoiceSerializer,
    PatientIntakeUpdateSerializer,
    PatientSerializer,
    SaveSquareCardSerializer,
    TerminalCheckoutSerializer,
    TerminalCheckoutStatusSerializer,
    PaymentCompleteSerializer,
    PaymentSerializer,
    PublicBookingSerializer,
    ProviderSerializer,
    ProviderUnavailabilitySerializer,
    ServiceSerializer,
    StaffNotificationSerializer,
    VisitCompleteSerializer,
    VisitSerializer,
    VoiceCallLogSerializer,
    complete_visit_with_services,
)
from .square_helpers import (
    get_application_id,
    get_location_id,
    get_terminal_device_id,
    save_card_from_source,
    square_configured,
)
from .google_calendar_sync import (
    build_oauth_flow,
    exchange_oauth_code,
    google_oauth_configured,
)
from .square_payment import (
    build_invoice_payment_followup_dict,
    create_terminal_checkout_for_invoice,
    get_terminal_checkout_status,
)
from .booking_availability import provider_interval_blocked_online

# Optional Square / card-on-file fields — defer on read-heavy querysets so SELECT does not
# reference missing columns if migrations have not been applied yet.
_PATIENT_OPTIONAL_CARD_FIELDS = (
    "square_customer_id",
    "square_card_id",
    "card_brand",
    "card_last4",
)


def _defer_patient_card_fields(qs, *, patient_prefix: str | None = None):
    """
    Omit optional payment columns from SQL (nested patient FK: use patient_prefix e.g. 'patient').
    """
    if patient_prefix:
        names = [f"{patient_prefix}__{f}" for f in _PATIENT_OPTIONAL_CARD_FIELDS]
    else:
        names = list(_PATIENT_OPTIONAL_CARD_FIELDS)
    return qs.defer(*names)


def _clinic_settings_bill_header():
    """Header fields for printed bills and API responses (single DB row)."""
    s = ClinicSettings.get_solo()
    return {
        "clinic_name": s.clinic_name,
        "address_line1": s.address_line1,
        "city_state_zip": s.city_state_zip,
        "phone": s.phone,
        "email": s.email or "",
        "pos_default": s.pos_default,
    }


def _can_edit_handoff_notes(request, appointment: Appointment) -> bool:
    role = getattr(request.user, "role", None)
    if role in ("owner_admin", "staff"):
        return True
    if role == "doctor":
        prov = Provider.objects.filter(user=request.user).first()
        return bool(prov and appointment.provider_id == prov.id)
    return False


def _serialize_patient_appointment_history(request, appointments):
    """Build chart rows for patient_detail (visits, billing lines, handoff notes)."""
    appt_list = list(appointments)
    if not appt_list:
        return []
    ids = [a.id for a in appt_list]
    visits = Visit.objects.filter(appointment_id__in=ids).prefetch_related(
        Prefetch(
            "rendered_services",
            queryset=VisitRenderedService.objects.select_related("service"),
        )
    )
    visits_by_aid = {v.appointment_id: v for v in visits}
    invoices_by_aid = {i.appointment_id: i for i in Invoice.objects.filter(appointment_id__in=ids)}
    out = []
    for a in appt_list:
        v = visits_by_aid.get(a.id)
        inv = invoices_by_aid.get(a.id)
        lines = []
        if v:
            for rs in v.rendered_services.all():
                lines.append(
                    {
                        "service_name": rs.service.name,
                        "billing_code": rs.service.billing_code or "",
                        "quantity": rs.quantity,
                        "unit_price": str(rs.unit_price),
                        "line_total": str(rs.total_price),
                    }
                )
        visit_payload = None
        if v:
            visit_payload = {
                "id": v.id,
                "status": v.status,
                "reason_for_visit": v.reason_for_visit or "",
                "doctor_notes": v.doctor_notes or "",
                "diagnosis": v.diagnosis or "",
                "completed_at": v.completed_at.isoformat() if v.completed_at else None,
                "rendered_services": lines,
            }
        inv_payload = None
        if inv:
            inv_payload = {
                "invoice_number": inv.invoice_number,
                "total_amount": str(inv.total_amount),
                "status": inv.status,
            }
        out.append(
            {
                "id": a.id,
                "appointment_date": str(a.appointment_date),
                "start_time": a.start_time.strftime("%I:%M %p"),
                "end_time": a.end_time.strftime("%I:%M %p"),
                "service": a.booked_service.name if a.booked_service else None,
                "provider": str(a.provider) if a.provider else None,
                "provider_id": a.provider_id,
                "status": a.status,
                "clinical_handoff_notes": a.clinical_handoff_notes or "",
                "can_edit_handoff_notes": _can_edit_handoff_notes(request, a),
                "visit": visit_payload,
                "invoice": inv_payload,
            }
        )
    return out


def _save_appointment_handoff_notes(request):
    ser = AppointmentHandoffNotesSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    aid = ser.validated_data["appointment_id"]
    notes = ser.validated_data["clinical_handoff_notes"]
    appt = Appointment.objects.filter(pk=aid).select_related("provider", "patient").first()
    if not appt:
        return Response({"detail": "Appointment not found."}, status=status.HTTP_404_NOT_FOUND)
    if not _can_edit_handoff_notes(request, appt):
        return Response(
            {"detail": "You cannot edit chart notes on this appointment."},
            status=status.HTTP_403_FORBIDDEN,
        )
    appt.clinical_handoff_notes = notes
    appt.save(update_fields=["clinical_handoff_notes", "updated_at"])
    return Response({"detail": "Saved.", "clinical_handoff_notes": appt.clinical_handoff_notes})


class BookingOptionsViewSet(viewsets.ViewSet):
    """Public endpoint: services and providers available for online booking."""

    permission_classes = [permissions.AllowAny]

    def list(self, request):
        bookable = (
            Service.objects.filter(is_active=True, show_in_public_booking=True)
            .annotate(
                _book_order=Case(
                    When(service_type=Service.ServiceType.CHIROPRACTIC, then=Value(0)),
                    When(service_type=Service.ServiceType.MASSAGE, then=Value(1)),
                    default=Value(2),
                    output_field=IntegerField(),
                )
            )
            .order_by("_book_order", "name")
        )
        services = list(
            bookable.values("id", "name", "duration_minutes", "price", "service_type")
        )
        providers_by_service = {}
        for svc in bookable.prefetch_related("providers"):
            providers_by_service[svc.id] = [
                {"id": p.id, "provider_name": str(p)}
                for p in svc.providers.filter(active=True)
            ]
        # Add allow_provider_choice: chiropractic = no choice (admin-assigned doctor), massage = patient chooses
        for s in services:
            s["allow_provider_choice"] = s.get("service_type") == "massage"
        return Response({"services": services, "providers_by_service": providers_by_service})

    @action(detail=False, methods=["get"], url_path="availability")
    def availability(self, request):
        """Return available time slots for a date/provider/service. Public."""
        from datetime import datetime

        date_str = request.query_params.get("date")
        provider_id = request.query_params.get("provider_id")
        service_id = request.query_params.get("service_id")
        if not all([date_str, provider_id, service_id]):
            return Response(
                {"detail": "date, provider_id, and service_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            appt_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return Response({"detail": "Invalid date format. Use YYYY-MM-DD."}, status=status.HTTP_400_BAD_REQUEST)
        provider = Provider.objects.filter(pk=provider_id, active=True).first()
        service = Service.objects.filter(pk=service_id, is_active=True, show_in_public_booking=True).first()
        if not provider or not service:
            return Response({"detail": "Invalid provider or service."}, status=status.HTTP_400_BAD_REQUEST)
        if not provider.services.filter(pk=service.id).exists():
            return Response({"detail": "Provider does not offer this service."}, status=status.HTTP_400_BAD_REQUEST)

        all_slots = [
            ("9:00 AM", (9, 0)),
            ("10:15 AM", (10, 15)),
            ("2:30 PM", (14, 30)),
            ("3:45 PM", (15, 45)),
            ("5:15 PM", (17, 15)),
        ]
        duration = service.duration_minutes

        taken = set()
        for a in (
            Appointment.objects.filter(
                provider=provider,
                appointment_date=appt_date,
            )
            .exclude(status__in=[Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW, Appointment.Status.COMPLETED])
            .values_list("start_time", "end_time")
        ):
            start_min = a[0].hour * 60 + a[0].minute
            end_min = a[1].hour * 60 + a[1].minute
            for m in range(start_min, end_min):
                taken.add(m)

        from datetime import time as time_cls

        available = []
        for label, (h, m) in all_slots:
            slot_start = h * 60 + m
            slot_end = slot_start + duration
            if any(slot_start <= t < slot_end for t in taken):
                continue
            slot_start_time = time_cls(hour=h, minute=m)
            total_end_min = slot_start + duration
            end_h = total_end_min // 60
            end_mm = total_end_min % 60
            if end_h >= 24:
                end_h, end_mm = 23, 59
            slot_end_time = time_cls(hour=end_h, minute=end_mm)
            if provider_interval_blocked_online(provider.pk, appt_date, slot_start_time, slot_end_time):
                continue
            available.append(label)
        return Response({"available_slots": available})

    @action(detail=False, methods=["get"], url_path="patient-lookup")
    def patient_lookup(self, request):
        """Look up existing patient by phone for pre-fill in booking. Public."""
        phone_raw = request.query_params.get("phone")
        if not phone_raw:
            return Response({"found": False})
        valid, _ = validate_phone(phone_raw)
        if not valid:
            return Response({"found": False})
        norm = normalize_phone(phone_raw)
        patient = Patient.objects.filter(phone=norm).first()
        if not patient:
            # Also try matching normalized against stored (for legacy data)
            for p in Patient.objects.all():
                if normalize_phone(p.phone) == norm:
                    patient = p
                    break
        if not patient:
            return Response({"found": False})
        return Response({
            "found": True,
            "first_name": patient.first_name,
            "last_name": patient.last_name,
            "email": patient.email or "",
            "has_saved_card": bool(patient.card_last4),
            "card_brand": patient.card_brand or "",
            "card_last4": patient.card_last4 or "",
        })

    @action(detail=False, methods=["get"], url_path="square-config")
    def square_config(self, request):
        """Public: whether Square is enabled + Web Payments SDK ids (https://developer.squareup.com/docs/web-payments/overview)."""
        from django.conf import settings as dj_settings

        env = (getattr(dj_settings, "SQUARE_ENVIRONMENT", None) or "sandbox").strip().lower()
        return Response(
            {
                "enabled": square_configured(),
                "application_id": get_application_id() if square_configured() else "",
                "location_id": get_location_id() if square_configured() else "",
                "environment": env if square_configured() else "",
            }
        )

    @action(detail=False, methods=["post"], url_path="save-card")
    def save_card(self, request):
        """Persist a card on file using a Web Payments token (source_id from card.tokenize())."""
        if not square_configured():
            return Response({"detail": "Card registration is not enabled yet."}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        ser = SaveSquareCardSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        phone_norm = normalize_phone(data["phone"])
        patient = Patient.objects.filter(phone=phone_norm).first()
        if not patient:
            fn = (data.get("first_name") or "").strip()
            ln = (data.get("last_name") or "").strip()
            if not fn or not ln:
                return Response(
                    {"detail": "Patient not found for this phone; include first_name and last_name to create the profile."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            patient = Patient.objects.create(
                phone=phone_norm,
                first_name=fn,
                last_name=ln,
                email=(data.get("email") or "").strip(),
            )
        src = data["source_id"]
        vtok = (data.get("verification_token") or "").strip() or None
        try:
            save_card_from_source(patient, src, verification_token=vtok)
        except Exception as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(
            {
                "detail": "Card saved.",
                "card_brand": patient.card_brand,
                "card_last4": patient.card_last4,
            }
        )


class IsOwnerOrDoctor(permissions.BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.role in {"owner_admin", "doctor", "staff"})


class IsStaffOrOwnerAdmin(permissions.BasePermission):
    """Owner and desk staff only (e.g. online booking blocks)."""

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and getattr(request.user, "role", None) in ("owner_admin", "staff")
        )


class PatientViewSet(viewsets.ModelViewSet):
    queryset = Patient.objects.all().order_by("-updated_at")
    serializer_class = PatientSerializer
    permission_classes = [IsOwnerOrDoctor]

    def destroy(self, request, *args, **kwargs):
        if getattr(request.user, "role", None) not in ("owner_admin", "staff"):
            return Response(
                {"detail": "Only clinic administrators (owner or staff) can delete a patient record."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return super().destroy(request, *args, **kwargs)


class ProviderViewSet(viewsets.ModelViewSet):
    queryset = Provider.objects.select_related("user").all().order_by("id")
    serializer_class = ProviderSerializer
    permission_classes = [IsOwnerOrDoctor]

    def destroy(self, request, *args, **kwargs):
        provider = self.get_object()
        if Appointment.objects.filter(provider=provider).exists() or Visit.objects.filter(provider=provider).exists():
            return Response(
                {
                    "detail": "This provider has appointments or visit history on file. Deactivate them instead of deleting, "
                    "or reassign/remove those records first."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        user = provider.user
        with transaction.atomic():
            self.perform_destroy(provider)
            user.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"], url_path="reassign-history")
    def reassign_history(self, request, pk=None):
        """Move all appointments and visits from this provider to another (owner/staff only). Then you can remove the provider."""
        if getattr(request.user, "role", None) not in ("owner_admin", "staff"):
            return Response(
                {"detail": "Only clinic administrators can transfer provider history."},
                status=status.HTTP_403_FORBIDDEN,
            )
        src = self.get_object()
        raw_tid = request.data.get("target_provider_id")
        if raw_tid is None:
            return Response({"detail": "target_provider_id is required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            tid = int(raw_tid)
        except (TypeError, ValueError):
            return Response({"detail": "target_provider_id must be an integer."}, status=status.HTTP_400_BAD_REQUEST)
        if tid == src.pk:
            return Response(
                {"detail": "Choose a different provider than the one you are transferring from."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        target = Provider.objects.filter(pk=tid).first()
        if not target:
            return Response({"detail": "Target provider not found."}, status=status.HTTP_404_NOT_FOUND)

        appt_count = Appointment.objects.filter(provider=src).count()
        visit_count = Visit.objects.filter(provider=src).count()
        if appt_count == 0 and visit_count == 0:
            return Response(
                {"detail": "This provider has no appointments or visits to transfer."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            Appointment.objects.filter(provider=src).update(provider=target)
            Visit.objects.filter(provider=src).update(provider=target)

        target_label = getattr(target.user, "full_name", None) or getattr(target.user, "username", None) or str(target.pk)
        return Response(
            {
                "detail": f"Transferred {appt_count} appointment(s) and {visit_count} visit(s) to {target_label}.",
                "appointments_moved": appt_count,
                "visits_moved": visit_count,
            }
        )


class ProviderUnavailabilityViewSet(viewsets.ModelViewSet):
    """Owner/staff: mark providers unavailable for public online booking (date or time window)."""

    queryset = ProviderUnavailability.objects.select_related("provider").all()
    serializer_class = ProviderUnavailabilitySerializer
    permission_classes = [IsStaffOrOwnerAdmin]
    http_method_names = ["get", "post", "delete", "head", "options"]

    def get_queryset(self):
        qs = super().get_queryset().order_by("-block_date", "start_time")
        pid = self.request.query_params.get("provider_id")
        if pid:
            try:
                qs = qs.filter(provider_id=int(pid))
            except (TypeError, ValueError):
                pass
        df = self.request.query_params.get("date_from")
        dt_to = self.request.query_params.get("date_to")
        if df:
            try:
                qs = qs.filter(block_date__gte=timezone.datetime.strptime(df, "%Y-%m-%d").date())
            except ValueError:
                pass
        if dt_to:
            try:
                qs = qs.filter(block_date__lte=timezone.datetime.strptime(dt_to, "%Y-%m-%d").date())
            except ValueError:
                pass
        return qs


class ServiceViewSet(viewsets.ModelViewSet):
    queryset = Service.objects.all().order_by("name")
    serializer_class = ServiceSerializer
    permission_classes = [IsOwnerOrDoctor]


class AppointmentViewSet(viewsets.ModelViewSet):
    queryset = _defer_patient_card_fields(
        Appointment.objects.select_related("patient", "provider", "booked_service").all().order_by(
            "appointment_date", "start_time"
        ),
        patient_prefix="patient",
    )
    serializer_class = AppointmentSerializer
    permission_classes = [IsOwnerOrDoctor]

    def get_serializer_class(self):
        if self.action == "list":
            return AppointmentListSerializer
        return AppointmentSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        if self.action != "list":
            return qs
        params = self.request.query_params
        if params.get("date_from"):
            try:
                qs = qs.filter(appointment_date__gte=timezone.datetime.strptime(params["date_from"], "%Y-%m-%d").date())
            except ValueError:
                pass
        if params.get("date_to"):
            try:
                qs = qs.filter(appointment_date__lte=timezone.datetime.strptime(params["date_to"], "%Y-%m-%d").date())
            except ValueError:
                pass
        if params.get("appointment_date"):
            try:
                qs = qs.filter(appointment_date=timezone.datetime.strptime(params["appointment_date"], "%Y-%m-%d").date())
            except ValueError:
                pass
        if params.get("provider_id"):
            qs = qs.filter(provider_id=params["provider_id"])
        if params.get("status"):
            qs = qs.filter(status=params["status"])
        return qs

    def get_permissions(self):
        if self.action == "book":
            return [permissions.AllowAny()]
        return super().get_permissions()

    def perform_create(self, serializer):
        appt = serializer.save()
        aid = appt.id

        def queue_calendar():
            from apps.notifications.tasks import sync_appointment_google_calendar_task

            sync_appointment_google_calendar_task.delay(aid)

        def queue_doctor_alert():
            from apps.notifications.tasks import notify_provider_new_booking_task

            notify_provider_new_booking_task.delay(aid)

        transaction.on_commit(queue_calendar)
        transaction.on_commit(queue_doctor_alert)

        def queue_in_app():
            from apps.clinic.in_app_notify import create_new_booking_in_app_notification

            create_new_booking_in_app_notification(aid)

        transaction.on_commit(queue_in_app)

    def perform_update(self, serializer):
        """If the visit time changes, allow a fresh day-before SMS reminder."""
        from datetime import datetime, timedelta

        inst = serializer.instance
        user = self.request.user
        role = getattr(user, "role", None)
        data = serializer.validated_data

        if role == "doctor":
            prov = Provider.objects.filter(user=user).first()
            if not prov or inst.provider_id != prov.id:
                raise PermissionDenied("You can only update appointments on your own schedule.")
            new_prov = data.get("provider", inst.provider)
            npid = new_prov.pk if hasattr(new_prov, "pk") else new_prov
            if npid != inst.provider_id:
                raise PermissionDenied("Only the front desk can assign this visit to another provider.")
            if "status" in data:
                new_s = data["status"]
                old_s = inst.status
                if new_s in (Appointment.Status.NO_SHOW, Appointment.Status.CANCELLED):
                    if old_s not in (
                        Appointment.Status.BOOKED,
                        Appointment.Status.CONFIRMED,
                        Appointment.Status.CHECKED_IN,
                    ):
                        raise PermissionDenied(
                            "You can only mark no-show or cancelled before the visit is in progress or finished."
                        )
                elif new_s == Appointment.Status.COMPLETED:
                    if old_s not in (
                        Appointment.Status.IN_CONSULTATION,
                        Appointment.Status.AWAITING_PAYMENT,
                    ):
                        raise PermissionDenied(
                            "Mark completed only when the visit is in progress or awaiting payment."
                        )
            if any(k in data for k in ("appointment_date", "start_time", "end_time")):
                if inst.status in (
                    Appointment.Status.IN_CONSULTATION,
                    Appointment.Status.AWAITING_PAYMENT,
                    Appointment.Status.COMPLETED,
                ):
                    raise PermissionDenied(
                        "Ask the front desk to reschedule a visit that is already in progress, awaiting payment, or completed."
                    )

        merged_date = data.get("appointment_date", inst.appointment_date)
        merged_start = data.get("start_time", inst.start_time)
        if ("appointment_date" in data or "start_time" in data) and "end_time" not in data:
            svc = data.get("booked_service", inst.booked_service)
            if svc is not None:
                start_dt = datetime.combine(merged_date, merged_start)
                end_dt = start_dt + timedelta(minutes=svc.duration_minutes)
                data["end_time"] = end_dt.time()

        merged_end = data.get("end_time", inst.end_time)
        prov_obj = data.get("provider", inst.provider)
        overlap_pid = prov_obj.pk if hasattr(prov_obj, "pk") else inst.provider_id
        if any(k in data for k in ("appointment_date", "start_time", "end_time", "provider")):
            overlapping = (
                Appointment.objects.filter(
                    provider_id=overlap_pid,
                    appointment_date=merged_date,
                    start_time__lt=merged_end,
                    end_time__gt=merged_start,
                )
                .exclude(pk=inst.pk)
                .exclude(
                    status__in=[
                        Appointment.Status.CANCELLED,
                        Appointment.Status.NO_SHOW,
                        Appointment.Status.COMPLETED,
                    ]
                )
                .exists()
            )
            if overlapping:
                raise ValidationError({"detail": "That time slot is already booked for this provider."})

        if data.get("status") == Appointment.Status.COMPLETED and inst.completed_at is None:
            data["completed_at"] = timezone.now()
        if data.get("status") in (Appointment.Status.NO_SHOW, Appointment.Status.CANCELLED):
            if inst.status in (
                Appointment.Status.BOOKED,
                Appointment.Status.CONFIRMED,
                Appointment.Status.CHECKED_IN,
            ):
                data["checked_in_at"] = None
                data["consultation_started_at"] = None

        old = {
            "appointment_date": inst.appointment_date,
            "start_time": inst.start_time,
            "end_time": inst.end_time,
            "status": inst.status,
            "provider_id": inst.provider_id,
            "booked_service_id": inst.booked_service_id,
        }
        date_changed = "appointment_date" in data and data["appointment_date"] != inst.appointment_date
        time_changed = "start_time" in data and data["start_time"] != inst.start_time
        if date_changed or time_changed:
            serializer.save(sms_reminder_sent_at=None)
        else:
            serializer.save()
        new = serializer.instance
        aid = new.id

        change_lines: list[str] = []
        if old["appointment_date"] != new.appointment_date:
            change_lines.append(f"Date: {old['appointment_date']} → {new.appointment_date}.")
        if old["start_time"] != new.start_time or old["end_time"] != new.end_time:
            change_lines.append(
                f"Time: {format_time_12h(old['start_time'])} → {format_time_12h(new.start_time)}."
            )
        if old["status"] != new.status:
            change_lines.append(f"Status: {old['status']} → {new.status}.")
        if old["booked_service_id"] != new.booked_service_id:
            change_lines.append("Booked service changed.")
        old_provider_id = None
        old_date_iso = None
        old_time_iso = None
        if old["provider_id"] != new.provider_id:
            change_lines.append("This appointment is now on your schedule (reassigned).")
            old_provider_id = old["provider_id"]
            old_date_iso = str(old["appointment_date"])
            old_time_iso = old["start_time"].isoformat()

        def queue_calendar():
            from apps.notifications.tasks import sync_appointment_google_calendar_task

            sync_appointment_google_calendar_task.delay(aid)

        def queue_doctor_alerts():
            from apps.notifications.tasks import notify_provider_schedule_change_task

            if change_lines:
                notify_provider_schedule_change_task.delay(
                    aid,
                    change_lines,
                    old_provider_id=old_provider_id,
                    old_date_iso=old_date_iso,
                    old_time_iso=old_time_iso,
                )

        def queue_in_app():
            from apps.clinic.in_app_notify import create_schedule_change_in_app_notifications

            if change_lines:
                create_schedule_change_in_app_notifications(
                    aid,
                    change_lines,
                    old_provider_id,
                    old_date_iso,
                    old_time_iso,
                )

        transaction.on_commit(queue_calendar)
        transaction.on_commit(queue_doctor_alerts)
        transaction.on_commit(queue_in_app)

    def perform_destroy(self, instance):
        from .google_calendar_sync import delete_appointment_google_event_before_db_delete

        delete_appointment_google_event_before_db_delete(instance)
        super().perform_destroy(instance)

    @action(detail=False, methods=["post"])
    def book(self, request):
        serializer = PublicBookingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = serializer.validated_data

        appointment, err = create_appointment_from_public_booking(payload)
        if err:
            code = (
                status.HTTP_409_CONFLICT
                if "slot" in err.lower() or "time is not open" in err.lower()
                else status.HTTP_400_BAD_REQUEST
            )
            return Response({"detail": err}, status=code)

        patient = appointment.patient
        service = appointment.booked_service
        provider = appointment.provider
        return Response(
            {
                "appointment_id": appointment.id,
                "status": appointment.status,
                "patient": f"{patient.first_name} {patient.last_name}",
                "provider": str(provider),
                "service": service.name,
                "service_type": service.service_type,
                "appointment_date": str(appointment.appointment_date),
                "start_time": appointment.start_time.strftime("%I:%M %p"),
                "total_amount": str(service.price),
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"])
    def start(self, request, pk=None):
        appointment = self.get_object()
        appointment.status = Appointment.Status.IN_CONSULTATION
        appointment.consultation_started_at = timezone.now()
        appointment.save(update_fields=["status", "consultation_started_at", "updated_at"])

        visit, _ = Visit.objects.get_or_create(
            appointment=appointment,
            defaults={
                "patient": appointment.patient,
                "provider": appointment.provider,
                "status": Visit.Status.IN_PROGRESS,
            },
        )
        if visit.status == Visit.Status.OPEN:
            visit.status = Visit.Status.IN_PROGRESS
            visit.save(update_fields=["status", "updated_at"])
        return Response({"appointment_status": appointment.status, "visit_id": visit.id})


class VisitViewSet(viewsets.ModelViewSet):
    queryset = Visit.objects.prefetch_related("rendered_services").all().order_by("-updated_at")
    serializer_class = VisitSerializer
    permission_classes = [IsOwnerOrDoctor]

    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        visit = self.get_object()
        serializer = VisitCompleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        invoice = complete_visit_with_services(visit, serializer.validated_data)
        return Response({"visit_status": visit.status, "invoice_id": invoice.id}, status=status.HTTP_201_CREATED)


class InvoiceViewSet(viewsets.ModelViewSet):
    queryset = _defer_patient_card_fields(
        Invoice.objects.select_related("patient", "appointment", "visit").all().order_by("-issued_at"),
        patient_prefix="patient",
    )
    serializer_class = InvoiceSerializer
    permission_classes = [IsOwnerOrDoctor]

    @action(detail=True, methods=["post"])
    def pay(self, request, pk=None):
        invoice = self.get_object()
        serializer = PaymentCompleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        payment = serializer.save(invoice=invoice)
        return Response(PaymentSerializer(payment).data, status=status.HTTP_201_CREATED)


class PaymentViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = _defer_patient_card_fields(
        Payment.objects.select_related("invoice", "patient").all().order_by("-created_at"),
        patient_prefix="patient",
    )
    serializer_class = PaymentSerializer
    permission_classes = [IsOwnerOrDoctor]


class AdminViewSet(viewsets.ViewSet):
    """Admin dashboard summary. Owner/staff only."""

    permission_classes = [IsOwnerOrDoctor]

    @action(detail=False, methods=["get"])
    def dashboard_summary(self, request):
        today = timezone.localdate()
        appts = _defer_patient_card_fields(
            Appointment.objects.filter(appointment_date=today)
            .select_related("patient", "provider", "booked_service")
            .exclude(status__in=[Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW])
            .order_by("start_time"),
            patient_prefix="patient",
        )

        appointments_today = appts.count()
        checked_in = appts.filter(status=Appointment.Status.CHECKED_IN).count()
        completed = appts.filter(status=Appointment.Status.COMPLETED).count()

        from django.db.models import Sum
        daily_revenue = Invoice.objects.filter(
            status=Invoice.Status.PAID,
            paid_at__date=today,
        ).aggregate(total=Sum("total_amount"))["total"] or 0

        unpaid_invoices = Invoice.objects.filter(status=Invoice.Status.ISSUED).count()

        today_schedule = []
        for a in appts[:20]:
            today_schedule.append({
                "id": a.id,
                "patient_name": f"{a.patient.first_name} {a.patient.last_name}",
                "provider_name": str(a.provider),
                "start_time": a.start_time.strftime("%I:%M %p"),
                "status": a.status,
            })

        recent_activity = []
        for a in _defer_patient_card_fields(
            Appointment.objects.select_related("patient").filter(appointment_date=today).order_by("-updated_at")[
                :10
            ],
            patient_prefix="patient",
        ):
            if a.status == Appointment.Status.CHECKED_IN:
                recent_activity.append(
                    f"{a.patient.first_name} {a.patient.last_name} checked in."
                )
            elif a.status == Appointment.Status.COMPLETED:
                recent_activity.append(
                    f"{a.patient.first_name} {a.patient.last_name} completed visit."
                )
        for p in _defer_patient_card_fields(
            Payment.objects.select_related("invoice__patient")
            .filter(paid_at__date=today)
            .order_by("-paid_at")[:5],
            patient_prefix="invoice__patient",
        ):
            recent_activity.append(
                f"Invoice paid by {p.invoice.patient.first_name} {p.invoice.patient.last_name}."
            )

        return Response({
            "appointments_today": appointments_today,
            "checked_in": checked_in,
            "completed": completed,
            "daily_revenue": str(daily_revenue),
            "unpaid_invoices": unpaid_invoices,
            "today_schedule": today_schedule,
            "recent_activity": recent_activity[:10],
        })

    def _admin_staff_only(self, request):
        if getattr(request.user, "role", None) not in ("owner_admin", "staff"):
            return Response(
                {"detail": "Only clinic administrators (owner or staff) can view voice analytics."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return None

    @action(detail=False, methods=["get"], url_path="voice_analytics")
    def voice_analytics(self, request):
        denied = self._admin_staff_only(request)
        if denied:
            return denied
        tz = ZoneInfo(getattr(settings, "CLINIC_TIMEZONE", "America/Detroit"))
        now_local = timezone.now().astimezone(tz)
        start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        qs = VoiceCallLog.objects.filter(created_at__gte=start, created_at__lt=end)
        calls_today = qs.count()
        booked = qs.filter(outcome=VoiceCallLog.Outcome.BOOKED).count()
        # Anything that did not end with a successful voice booking (includes hang-ups on greeting).
        escalated_or_failed = qs.exclude(outcome=VoiceCallLog.Outcome.BOOKED).count()
        booked_rows = qs.filter(outcome=VoiceCallLog.Outcome.BOOKED)
        avg_sec = None
        if booked_rows.exists():
            total = 0.0
            n = 0
            for row in booked_rows:
                delta = (row.updated_at - row.created_at).total_seconds()
                if delta >= 0:
                    total += delta
                    n += 1
            if n:
                avg_sec = int(total / n)
        return Response({
            "calls_today": calls_today,
            "booked_by_voice": booked,
            "escalated_or_failed": escalated_or_failed,
            "avg_handle_seconds": avg_sec,
            "openai_configured": bool((getattr(settings, "OPENAI_API_KEY", "") or "").strip()),
        })

    @action(detail=False, methods=["get"], url_path="voice_calls")
    def voice_calls(self, request):
        denied = self._admin_staff_only(request)
        if denied:
            return denied
        limit = min(int(request.query_params.get("limit", 50)), 100)
        qs = VoiceCallLog.objects.all().order_by("-updated_at")[:limit]
        return Response(VoiceCallLogSerializer(qs, many=True).data)

    @action(detail=False, methods=["get", "patch"], url_path="clinic_profile")
    def clinic_profile(self, request):
        """Clinic name, address, phone, hours, and bill POS code (admin Settings); persisted in DB."""
        solo = ClinicSettings.get_solo()
        if request.method == "GET":
            h = _clinic_settings_bill_header()
            return Response({
                **h,
                "business_hours": list(solo.business_hours or []),
            })
        if getattr(request.user, "role", None) not in ("owner_admin", "staff"):
            return Response(
                {"detail": "Only clinic administrators (owner or staff) can update clinic settings."},
                status=status.HTTP_403_FORBIDDEN,
            )
        ser = ClinicProfileUpdateSerializer(data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        for field in (
            "clinic_name",
            "address_line1",
            "city_state_zip",
            "phone",
            "email",
            "pos_default",
        ):
            if field in data:
                val = data[field]
                setattr(solo, field, (val or "") if field == "email" else val)
        if "business_hours" in data:
            solo.business_hours = data["business_hours"]
        solo.save()
        h = _clinic_settings_bill_header()
        return Response({
            **h,
            "business_hours": list(solo.business_hours or []),
        })

    @action(detail=False, methods=["get"], url_path="billing_invoices")
    def billing_invoices(self, request):
        """Invoices for admin billing UI (patient name + totals)."""
        qs = _defer_patient_card_fields(
            Invoice.objects.select_related("patient").order_by("-issued_at"),
            patient_prefix="patient",
        )[:250]
        return Response(
            [
                {
                    "id": inv.id,
                    "invoice_number": inv.invoice_number,
                    "patient_id": inv.patient_id,
                    "patient_name": f"{inv.patient.first_name} {inv.patient.last_name}",
                    "status": inv.status,
                    "total_amount": str(inv.total_amount),
                    "subtotal": str(inv.subtotal),
                    "tax": str(inv.tax),
                    "issued_at": inv.issued_at.isoformat() if inv.issued_at else None,
                    "paid_at": inv.paid_at.isoformat() if inv.paid_at else None,
                }
                for inv in qs
            ]
        )

    @action(detail=False, methods=["get"])
    def patients(self, request):
        """List all patients with last_visit and balance for admin."""
        from django.db.models import Sum

        patients_qs = _defer_patient_card_fields(Patient.objects.all()).order_by("last_name", "first_name")
        data = []
        for p in patients_qs:
            last_appt = (
                Appointment.objects.filter(patient=p)
                .order_by("-appointment_date", "-start_time")
                .first()
            )
            balance_result = (
                Invoice.objects.filter(patient=p, status=Invoice.Status.ISSUED)
                .aggregate(total=Sum("total_amount"))["total"]
                or 0
            )
            data.append({
                "id": p.id,
                "first_name": p.first_name,
                "last_name": p.last_name,
                "phone": p.phone,
                "email": p.email or "",
                "last_visit": str(last_appt.appointment_date) if last_appt else None,
                "balance": str(balance_result),
            })
        return Response(data)

    @action(detail=False, methods=["get"], url_path="patient_detail")
    def patient_detail(self, request):
        """Get a patient's details with full appointment history. Admin can view any patient."""
        patient_id = request.query_params.get("patient_id")
        if not patient_id:
            return Response({"detail": "patient_id is required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            patient_id = int(patient_id)
        except (ValueError, TypeError):
            return Response({"detail": "Invalid patient_id."}, status=status.HTTP_400_BAD_REQUEST)
        patient = Patient.objects.filter(pk=patient_id).first()
        if not patient:
            return Response({"detail": "Patient not found."}, status=status.HTTP_404_NOT_FOUND)
        appointments = (
            Appointment.objects.filter(patient=patient)
            .select_related("booked_service", "provider")
            .order_by("-appointment_date", "-start_time")[:100]
        )
        return Response({
            "id": patient.id,
            "first_name": patient.first_name,
            "last_name": patient.last_name,
            "phone": patient.phone,
            "email": patient.email or "",
            "date_of_birth": str(patient.date_of_birth) if patient.date_of_birth else None,
            "address_line1": patient.address_line1 or "",
            "address_line2": patient.address_line2 or "",
            "city_state_zip": patient.city_state_zip or "",
            "emergency_contact_name": patient.emergency_contact_name or "",
            "emergency_contact_phone": patient.emergency_contact_phone or "",
            "card_brand": patient.card_brand or "",
            "card_last4": patient.card_last4 or "",
            "has_saved_card": bool(patient.card_last4),
            "appointments": _serialize_patient_appointment_history(request, appointments),
        })

    @action(detail=False, methods=["patch"], url_path="patient_intake")
    def patient_intake(self, request):
        """Update intake / demographics for any patient (owner/staff/doctor with admin access)."""
        patient_id = request.data.get("patient_id")
        if not patient_id:
            return Response({"detail": "patient_id is required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            patient_id = int(patient_id)
        except (ValueError, TypeError):
            return Response({"detail": "Invalid patient_id."}, status=status.HTTP_400_BAD_REQUEST)
        patient = Patient.objects.filter(pk=patient_id).first()
        if not patient:
            return Response({"detail": "Patient not found."}, status=status.HTTP_404_NOT_FOUND)
        ser = PatientIntakeUpdateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        for field in (
            "address_line1",
            "address_line2",
            "city_state_zip",
            "emergency_contact_name",
            "emergency_contact_phone",
        ):
            if field in data:
                setattr(patient, field, data[field] or "")
        if "date_of_birth" in data:
            patient.date_of_birth = data["date_of_birth"]
        patient.save()
        return Response({"detail": "Saved."})

    @action(detail=False, methods=["patch"], url_path="appointment_handoff")
    def appointment_handoff(self, request):
        """Save per-appointment chart / handoff notes (owner/staff may edit any appointment)."""
        if getattr(request.user, "role", None) not in ("owner_admin", "staff"):
            return Response(
                {"detail": "Only clinic administrators can use this endpoint."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return _save_appointment_handoff_notes(request)


class IsDoctor(permissions.BasePermission):
    """Only allow users with role=doctor."""

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.role == "doctor")


class DoctorViewSet(viewsets.ViewSet):
    """Doctor-only endpoints. All data filtered by the logged-in doctor's provider."""

    permission_classes = [IsDoctor]

    def _get_provider(self, request):
        provider = Provider.objects.filter(user=request.user).first()
        if not provider:
            return None
        return provider

    @action(detail=False, methods=["get"])
    def me(self, request):
        provider = self._get_provider(request)
        if not provider:
            return Response({"detail": "No provider linked to this account."}, status=status.HTTP_403_FORBIDDEN)
        return Response(
            {
                "provider_id": provider.id,
                "provider_name": str(provider),
                "full_name": request.user.full_name or request.user.username,
            }
        )

    @action(detail=False, methods=["get"])
    def appointments(self, request):
        provider = self._get_provider(request)
        if not provider:
            return Response({"detail": "No provider linked."}, status=status.HTTP_403_FORBIDDEN)
        date_str = request.query_params.get("date")
        if date_str:
            try:
                appt_date = timezone.datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                appt_date = timezone.localdate()
        else:
            appt_date = timezone.localdate()
        qs = (
            Appointment.objects.filter(provider=provider, appointment_date=appt_date)
            .select_related("patient", "booked_service")
            .order_by("start_time")
        )
        appts_today = list(qs)
        appt_ids = [x.id for x in appts_today]
        data = []
        visit_by_aid = {
            v.appointment_id: v
            for v in Visit.objects.filter(appointment_id__in=appt_ids).only(
                "id", "appointment_id", "reason_for_visit"
            )
        }
        invoice_by_aid = {
            inv.appointment_id: inv
            for inv in Invoice.objects.filter(appointment_id__in=appt_ids).only(
                "id", "appointment_id", "invoice_number", "total_amount", "status"
            )
        }
        for a in appts_today:
            v = visit_by_aid.get(a.id)
            row = {
                "id": a.id,
                "patient": f"{a.patient.first_name} {a.patient.last_name}",
                "patient_id": a.patient_id,
                "service": a.booked_service.name if a.booked_service else "",
                "booked_service_id": a.booked_service_id,
                "appointment_date": str(a.appointment_date),
                "start_time": a.start_time.strftime("%I:%M %p"),
                "start_time_iso": a.start_time.isoformat(timespec="seconds"),
                "end_time": a.end_time.strftime("%I:%M %p"),
                "status": a.status,
                "clinical_handoff_notes": a.clinical_handoff_notes or "",
                "reason_for_visit": v.reason_for_visit if v else "",
                "visit_id": v.id if v else None,
            }
            inv = invoice_by_aid.get(a.id)
            if (
                a.status == Appointment.Status.AWAITING_PAYMENT
                and inv
                and inv.status in (Invoice.Status.ISSUED, Invoice.Status.OVERDUE, Invoice.Status.DRAFT)
            ):
                row["invoice_id"] = inv.id
                row["invoice_number"] = inv.invoice_number
                row["invoice_total"] = str(inv.total_amount)
            data.append(row)
        return Response(data)

    @action(detail=False, methods=["get"])
    def patients(self, request):
        provider = self._get_provider(request)
        if not provider:
            return Response({"detail": "No provider linked."}, status=status.HTTP_403_FORBIDDEN)
        patient_ids = list(
            Appointment.objects.filter(provider=provider).values_list("patient_id", flat=True).distinct()
        )
        if not patient_ids:
            return Response([])
        patients_qs = _defer_patient_card_fields(
            Patient.objects.filter(id__in=patient_ids).order_by("last_name", "first_name")
        )
        today = timezone.localdate()
        ex_future = [Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW, Appointment.Status.COMPLETED]

        appts_all = list(
            Appointment.objects.filter(provider=provider, patient_id__in=patient_ids).order_by(
                "-appointment_date", "-start_time"
            )
        )
        last_any_by_pid: dict = {}
        for a in appts_all:
            last_any_by_pid.setdefault(a.patient_id, a)

        future_ordered = list(
            Appointment.objects.filter(
                provider=provider, patient_id__in=patient_ids, appointment_date__gte=today
            )
            .exclude(status__in=ex_future)
            .order_by("appointment_date", "start_time")
        )
        next_by_pid: dict = {}
        for a in future_ordered:
            next_by_pid.setdefault(a.patient_id, a)

        visits_done = list(
            Visit.objects.filter(
                provider=provider,
                patient_id__in=patient_ids,
                status=Visit.Status.COMPLETED,
                completed_at__isnull=False,
            ).order_by("-completed_at")
        )
        last_completed_by_pid: dict = {}
        for v in visits_done:
            last_completed_by_pid.setdefault(v.patient_id, v)

        open_inv_patient_ids = set(
            Invoice.objects.filter(
                patient_id__in=patient_ids,
                visit__provider=provider,
                status__in=[Invoice.Status.ISSUED, Invoice.Status.OVERDUE],
            ).values_list("patient_id", flat=True)
        )

        seen_since = timezone.now() - timezone.timedelta(days=30)
        seen_30_patient_ids = set(
            Visit.objects.filter(
                provider=provider,
                patient_id__in=patient_ids,
                status=Visit.Status.COMPLETED,
                completed_at__gte=seen_since,
            ).values_list("patient_id", flat=True)
        )

        data = []
        for p in patients_qs:
            na = next_by_pid.get(p.id)
            la = last_any_by_pid.get(p.id)
            lv = last_completed_by_pid.get(p.id)
            if lv and lv.completed_at:
                last_visit_iso = lv.completed_at.date().isoformat()
            elif la:
                last_visit_iso = str(la.appointment_date)
            else:
                last_visit_iso = None

            next_appt_str = None
            next_status = None
            if na:
                next_appt_str = f"{na.appointment_date} {na.start_time.strftime('%I:%M %p')}"
                next_status = na.status

            data.append(
                {
                    "id": p.id,
                    "name": f"{p.first_name} {p.last_name}".strip() or "Patient",
                    "phone": p.phone or "",
                    "email": (p.email or "").strip(),
                    "last_visit": last_visit_iso,
                    "next_appt": next_appt_str,
                    "next_appointment_status": next_status,
                    "has_upcoming": bool(na),
                    "has_open_invoice": p.id in open_inv_patient_ids,
                    "seen_last_30_days": p.id in seen_30_patient_ids,
                }
            )
        return Response(data)

    @action(detail=False, methods=["get"], url_path="patient_detail")
    def patient_detail(self, request):
        """Get a patient's details (only patients the doctor has seen or has appointments with)."""
        provider = self._get_provider(request)
        if not provider:
            return Response({"detail": "No provider linked."}, status=status.HTTP_403_FORBIDDEN)
        patient_id = request.query_params.get("patient_id")
        if not patient_id:
            return Response({"detail": "patient_id is required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            patient_id = int(patient_id)
        except (ValueError, TypeError):
            return Response({"detail": "Invalid patient_id."}, status=status.HTTP_400_BAD_REQUEST)
        has_access = Appointment.objects.filter(provider=provider, patient_id=patient_id).exists()
        if not has_access:
            return Response({"detail": "Patient not found."}, status=status.HTTP_404_NOT_FOUND)
        patient = Patient.objects.filter(pk=patient_id).select_related().first()
        if not patient:
            return Response({"detail": "Patient not found."}, status=status.HTTP_404_NOT_FOUND)
        # Full clinic timeline so any treating doctor can see colleagues' visits and chart notes.
        appointments = (
            Appointment.objects.filter(patient=patient)
            .select_related("booked_service", "provider")
            .order_by("-appointment_date", "-start_time")[:100]
        )
        return Response(
            {
                "id": patient.id,
                "first_name": patient.first_name,
                "last_name": patient.last_name,
                "phone": patient.phone,
                "email": patient.email or "",
                "date_of_birth": str(patient.date_of_birth) if patient.date_of_birth else None,
                "address_line1": patient.address_line1 or "",
                "address_line2": patient.address_line2 or "",
                "city_state_zip": patient.city_state_zip or "",
                "emergency_contact_name": patient.emergency_contact_name or "",
                "emergency_contact_phone": patient.emergency_contact_phone or "",
                "card_brand": patient.card_brand or "",
                "card_last4": patient.card_last4 or "",
                "has_saved_card": bool(patient.card_last4),
                "appointments": _serialize_patient_appointment_history(request, appointments),
            }
        )

    @action(detail=False, methods=["patch"], url_path="patient_intake")
    def patient_intake(self, request):
        """Update intake / address fields for a patient the doctor has seen."""
        provider = self._get_provider(request)
        if not provider:
            return Response({"detail": "No provider linked."}, status=status.HTTP_403_FORBIDDEN)
        patient_id = request.data.get("patient_id")
        if not patient_id:
            return Response({"detail": "patient_id is required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            patient_id = int(patient_id)
        except (ValueError, TypeError):
            return Response({"detail": "Invalid patient_id."}, status=status.HTTP_400_BAD_REQUEST)
        if not Appointment.objects.filter(provider=provider, patient_id=patient_id).exists():
            return Response({"detail": "Patient not found."}, status=status.HTTP_404_NOT_FOUND)
        patient = Patient.objects.filter(pk=patient_id).first()
        ser = PatientIntakeUpdateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        for field in (
            "address_line1",
            "address_line2",
            "city_state_zip",
            "emergency_contact_name",
            "emergency_contact_phone",
        ):
            if field in data:
                setattr(patient, field, data[field] or "")
        if "date_of_birth" in data:
            patient.date_of_birth = data["date_of_birth"]
        patient.save()
        return Response({"detail": "Saved."})

    @action(detail=False, methods=["patch"], url_path="appointment_handoff")
    def appointment_handoff(self, request):
        """Save chart / handoff notes on appointments assigned to this doctor."""
        return _save_appointment_handoff_notes(request)

    @action(detail=False, methods=["get"], url_path="invoice_payment_status")
    def invoice_payment_status(self, request):
        """Whether an invoice is paid (for print gating after Checkout / Terminal / webhook delay)."""
        provider = self._get_provider(request)
        if not provider:
            return Response({"detail": "No provider linked."}, status=status.HTTP_403_FORBIDDEN)
        invoice_id = request.query_params.get("invoice_id")
        if not invoice_id:
            return Response({"detail": "invoice_id is required."}, status=status.HTTP_400_BAD_REQUEST)
        inv = Invoice.objects.filter(pk=invoice_id).select_related("appointment").first()
        if not inv:
            return Response({"detail": "Invoice not found."}, status=status.HTTP_404_NOT_FOUND)
        if inv.appointment.provider_id != provider.id:
            return Response({"detail": "Not allowed."}, status=status.HTTP_403_FORBIDDEN)
        paid = inv.status == Invoice.Status.PAID
        return Response({"paid": paid, "status": inv.status})

    @action(detail=False, methods=["get"], url_path="invoice_bill")
    def invoice_bill(self, request):
        """Print-ready patient bill (matches clinic statement layout) for doctor's own invoice. Only after payment."""
        provider = self._get_provider(request)
        if not provider:
            return Response({"detail": "No provider linked."}, status=status.HTTP_403_FORBIDDEN)
        invoice_id = request.query_params.get("invoice_id")
        if not invoice_id:
            return Response({"detail": "invoice_id is required."}, status=status.HTTP_400_BAD_REQUEST)
        inv = (
            Invoice.objects.select_related("patient", "appointment", "visit")
            .prefetch_related("visit__rendered_services__service")
            .filter(pk=invoice_id)
            .first()
        )
        if not inv:
            return Response({"detail": "Invoice not found."}, status=status.HTTP_404_NOT_FOUND)
        if inv.appointment.provider_id != provider.id:
            return Response({"detail": "Not allowed."}, status=status.HTTP_403_FORBIDDEN)
        if inv.status != Invoice.Status.PAID:
            return Response(
                {
                    "detail": "Patient bill printing is available only after the invoice is paid. "
                    "Finish card payment or desk checkout first.",
                    "invoice_status": inv.status,
                },
                status=status.HTTP_409_CONFLICT,
            )
        visit = inv.visit
        header = _clinic_settings_bill_header()
        lines = []
        for rs in visit.rendered_services.all().order_by("id"):
            svc = rs.service
            lines.append(
                {
                    "service_offered": svc.name,
                    "cpt_code": svc.billing_code or "—",
                    "description": (svc.description or svc.name)[:120],
                    "fees": str(rs.unit_price),
                    "units": str(rs.quantity),
                    "pos": header["pos_default"],
                    "line_total": str(rs.total_price),
                }
            )
        pat = inv.patient
        addr_display = pat.city_state_zip or "St Joseph, MI 49085"
        if pat.address_line1:
            addr_display = ", ".join(filter(None, [pat.address_line1, pat.city_state_zip])) or addr_display
        return Response(
            {
                **header,
                "bill_title": "Patient Bill",
                "invoice_number": inv.invoice_number,
                "date_of_service": str(inv.appointment.appointment_date),
                "patient_name": f"{pat.first_name} {pat.last_name}",
                "patient_address": addr_display,
                "diagnosis": (visit.diagnosis or "").strip() or "—",
                "lines": lines,
                "subtotal": str(inv.subtotal),
                "tax": str(inv.tax),
                "total_amount": str(inv.total_amount),
                "status": inv.status,
            }
        )

    @action(detail=True, methods=["post"])
    def start_visit(self, request, pk=None):
        """Start consultation for an appointment (doctor must own it)."""
        provider = self._get_provider(request)
        if not provider:
            return Response({"detail": "No provider linked."}, status=status.HTTP_403_FORBIDDEN)
        appointment = Appointment.objects.filter(pk=pk, provider=provider).first()
        if not appointment:
            return Response({"detail": "Appointment not found."}, status=status.HTTP_404_NOT_FOUND)
        appointment.status = Appointment.Status.IN_CONSULTATION
        appointment.consultation_started_at = timezone.now()
        appointment.save(update_fields=["status", "consultation_started_at", "updated_at"])
        visit, _ = Visit.objects.get_or_create(
            appointment=appointment,
            defaults={"patient": appointment.patient, "provider": provider, "status": Visit.Status.IN_PROGRESS},
        )
        if visit.status == Visit.Status.OPEN:
            visit.status = Visit.Status.IN_PROGRESS
            visit.save(update_fields=["status", "updated_at"])
        return Response({"visit_id": visit.id})

    @action(detail=True, methods=["post"])
    def complete_visit(self, request, pk=None):
        """Complete a visit (doctor must own it). pk = appointment_id. Body: doctor_notes, diagnosis, rendered_services[]."""
        provider = self._get_provider(request)
        if not provider:
            return Response({"detail": "No provider linked."}, status=status.HTTP_403_FORBIDDEN)
        visit = Visit.objects.filter(appointment_id=pk, provider=provider).select_related("appointment__booked_service").first()
        if not visit:
            return Response({"detail": "Visit not found."}, status=status.HTTP_404_NOT_FOUND)
        ser = DoctorCompleteVisitSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        rendered_payload = []
        for line in data["rendered_services"]:
            svc = Service.objects.get(pk=line["service_id"])
            unit = line["unit_price"] if line.get("unit_price") is not None else svc.price
            rendered_payload.append(
                {
                    "service_id": svc.id,
                    "quantity": line.get("quantity", 1),
                    "unit_price": str(unit),
                }
            )
        payload = {
            "doctor_notes": data.get("doctor_notes", ""),
            "diagnosis": data.get("diagnosis", ""),
            "rendered_services": rendered_payload,
        }
        invoice = complete_visit_with_services(visit, payload)
        visit.appointment.status = Appointment.Status.AWAITING_PAYMENT
        visit.appointment.completed_at = timezone.now()
        visit.appointment.save(update_fields=["status", "completed_at", "updated_at"])

        invoice.refresh_from_db()
        followup = build_invoice_payment_followup_dict(
            invoice, try_saved_card=data.get("charge_saved_card_if_present", True)
        )
        followup.pop("already_paid", None)
        return Response(followup)

    @action(detail=False, methods=["post"], url_path="prepare_invoice_payment")
    def prepare_invoice_payment(self, request):
        """Re-open payment options for a visit stuck in awaiting_payment (e.g. banner was dismissed)."""
        provider = self._get_provider(request)
        if not provider:
            return Response({"detail": "No provider linked."}, status=status.HTTP_403_FORBIDDEN)
        raw_aid = request.data.get("appointment_id")
        if raw_aid is None:
            return Response({"detail": "appointment_id is required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            appointment_id = int(raw_aid)
        except (TypeError, ValueError):
            return Response({"detail": "Invalid appointment_id."}, status=status.HTTP_400_BAD_REQUEST)

        appt = Appointment.objects.filter(pk=appointment_id, provider=provider).first()
        if not appt:
            return Response({"detail": "Appointment not found."}, status=status.HTTP_404_NOT_FOUND)
        if appt.status != Appointment.Status.AWAITING_PAYMENT:
            return Response(
                {"detail": "Only visits waiting on payment can use this action."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        inv = Invoice.objects.filter(appointment=appt).first()
        if not inv:
            return Response({"detail": "No invoice for this visit."}, status=status.HTTP_404_NOT_FOUND)

        try_saved_card = bool(request.data.get("try_saved_card", False))
        followup = build_invoice_payment_followup_dict(inv, try_saved_card=try_saved_card)
        return Response(followup)

    @action(detail=False, methods=["get"], url_path="square_terminal_config")
    def square_terminal_config(self, request):
        """Doctor UI: Square location + whether a Terminal device id is configured."""
        provider = self._get_provider(request)
        if not provider:
            return Response({"detail": "No provider linked."}, status=status.HTTP_403_FORBIDDEN)
        if not square_configured():
            return Response({"detail": "Square is not configured."}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        loc = get_location_id()
        dev = get_terminal_device_id()
        return Response(
            {
                "location_id": loc,
                "has_location": bool(loc),
                "device_id_configured": bool(dev),
            }
        )

    @action(detail=False, methods=["post"], url_path="terminal_checkout")
    def terminal_checkout(self, request):
        """Create a Square Terminal checkout (in-person) for an unpaid invoice."""
        provider = self._get_provider(request)
        if not provider:
            return Response({"detail": "No provider linked."}, status=status.HTTP_403_FORBIDDEN)
        if not square_configured():
            return Response({"detail": "Square is not configured."}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        ser = TerminalCheckoutSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        inv = (
            Invoice.objects.select_related("appointment")
            .filter(pk=ser.validated_data["invoice_id"])
            .first()
        )
        if not inv:
            return Response({"detail": "Invoice not found."}, status=status.HTTP_404_NOT_FOUND)
        if inv.appointment.provider_id != provider.id:
            return Response({"detail": "Not allowed."}, status=status.HTTP_403_FORBIDDEN)
        if inv.status != Invoice.Status.ISSUED:
            return Response(
                {"detail": "Invoice is not awaiting payment."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            out = create_terminal_checkout_for_invoice(inv)
        except Exception as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(out)

    @action(detail=False, methods=["get"], url_path="terminal_checkout_status")
    def terminal_checkout_status(self, request):
        """Poll Terminal checkout status; marks invoice paid when checkout completes."""
        provider = self._get_provider(request)
        if not provider:
            return Response({"detail": "No provider linked."}, status=status.HTTP_403_FORBIDDEN)
        if not square_configured():
            return Response({"detail": "Square is not configured."}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        ser = TerminalCheckoutStatusSerializer(data=request.query_params)
        ser.is_valid(raise_exception=True)
        cid = ser.validated_data["checkout_id"]
        try:
            out = get_terminal_checkout_status(cid)
        except Exception as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(out)

    @action(detail=False, methods=["get"], url_path="google_calendar/status")
    def google_calendar_status(self, request):
        """Whether server OAuth is configured and this doctor has connected a personal Google account."""
        provider = self._get_provider(request)
        if not provider:
            return Response({"detail": "No provider linked."}, status=status.HTTP_403_FORBIDDEN)
        connected = bool((provider.google_refresh_token or "").strip())
        return Response(
            {
                "oauth_configured": google_oauth_configured(),
                "connected": connected,
            }
        )

    @action(detail=False, methods=["get"], url_path="google_calendar/oauth/start")
    def google_calendar_oauth_start(self, request):
        """Returns Google authorization URL (doctor opens in browser to connect personal Calendar)."""
        provider = self._get_provider(request)
        if not provider:
            return Response({"detail": "No provider linked."}, status=status.HTTP_403_FORBIDDEN)
        if not google_oauth_configured():
            return Response(
                {"detail": "Google Calendar OAuth is not configured on the server."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        try:
            flow = build_oauth_flow()
        except Exception as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        signer = TimestampSigner(salt="google-calendar-oauth")
        state = signer.sign(str(request.user.id))
        authorization_url, _ = flow.authorization_url(
            access_type="offline",
            prompt="consent",
            include_granted_scopes="true",
            state=state,
        )
        return Response({"authorization_url": authorization_url})

    @action(
        detail=False,
        methods=["get"],
        url_path="google_calendar/oauth/callback",
        permission_classes=[permissions.AllowAny],
        authentication_classes=[],
    )
    def google_calendar_oauth_callback(self, request):
        """OAuth redirect target (no JWT). State carries signed doctor user id."""
        from urllib.parse import urlencode

        base = getattr(settings, "FRONTEND_BASE_URL", "http://localhost:3000").rstrip("/")

        def redir(params: dict):
            return HttpResponseRedirect(f"{base}/doctor/schedule?{urlencode(params)}")

        if not google_oauth_configured():
            return redir({"google_calendar": "error", "reason": "config"})
        err = request.query_params.get("error")
        if err:
            return redir({"google_calendar": "error", "reason": err})
        code = request.query_params.get("code")
        state = request.query_params.get("state")
        if not code or not state:
            return redir({"google_calendar": "error", "reason": "missing_code"})
        try:
            exchange_oauth_code(
                authorization_response_url=request.build_absolute_uri(),
                state=state,
            )
        except ValueError as exc:
            return redir({"google_calendar": "error", "reason": str(exc)[:120]})
        except Exception:
            return redir({"google_calendar": "error", "reason": "oauth_failed"})
        return redir({"google_calendar": "connected"})

    @action(detail=False, methods=["post"], url_path="google_calendar/disconnect")
    def google_calendar_disconnect(self, request):
        """Remove stored Google tokens for this doctor (events are not deleted from Google)."""
        provider = self._get_provider(request)
        if not provider:
            return Response({"detail": "No provider linked."}, status=status.HTTP_403_FORBIDDEN)
        provider.google_refresh_token = ""
        provider.save(update_fields=["google_refresh_token", "updated_at"])
        return Response({"detail": "Disconnected from Google Calendar."})


class StaffNotificationViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """In-app bell: list + unread count + mark read (recipient = current user)."""

    serializer_class = StaffNotificationSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return StaffNotification.objects.filter(recipient=self.request.user).order_by("-created_at")

    def list(self, request, *args, **kwargs):
        qs = self.filter_queryset(self.get_queryset())[:40]
        serializer = self.get_serializer(qs, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=["get"], url_path="unread-count")
    def unread_count(self, request):
        n = StaffNotification.objects.filter(recipient=request.user, read_at__isnull=True).count()
        return Response({"unread_count": n})

    @action(detail=True, methods=["post"])
    def mark_read(self, request, pk=None):
        n = get_object_or_404(StaffNotification, pk=pk, recipient=request.user)
        if n.read_at is None:
            n.read_at = timezone.now()
            n.save(update_fields=["read_at", "updated_at"])
        return Response(StaffNotificationSerializer(n).data)

    @action(detail=False, methods=["post"], url_path="mark_all_read")
    def mark_all_read(self, request):
        StaffNotification.objects.filter(recipient=request.user, read_at__isnull=True).update(
            read_at=timezone.now()
        )
        return Response({"detail": "ok"})


class KioskViewSet(viewsets.ViewSet):
    """Public kiosk: patient lookup and check-in by phone."""

    permission_classes = [permissions.AllowAny]

    @action(detail=False, methods=["post"])
    def lookup(self, request):
        phone = request.data.get("phone")
        today = timezone.localdate()
        valid, msg = validate_phone(phone or "")
        if not valid:
            return Response({"detail": msg}, status=status.HTTP_400_BAD_REQUEST)
        norm = normalize_phone(phone)
        # Match by normalized phone (handles +1, 5551234567, etc.)
        appts = list(
            Appointment.objects.select_related("patient", "provider")
            .filter(appointment_date=today)
            .exclude(status__in=[Appointment.Status.CANCELLED, Appointment.Status.NO_SHOW])
            .order_by("start_time")
        )
        appt = next((a for a in appts if normalize_phone(a.patient.phone) == norm), None)
        if not appt:
            return Response({"detail": "Appointment not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(
            {
                "appointment_id": appt.id,
                "patient": f"{appt.patient.first_name} {appt.patient.last_name}",
                "provider": str(appt.provider),
                "time": str(appt.start_time),
                "status": appt.status,
            }
        )

    @action(detail=False, methods=["post"])
    def checkin(self, request):
        appointment_id = request.data.get("appointment_id")
        if appointment_id is None:
            return Response({"detail": "appointment_id is required."}, status=status.HTTP_400_BAD_REQUEST)
        appt = get_object_or_404(Appointment, pk=appointment_id)
        appt.status = Appointment.Status.CHECKED_IN
        appt.checked_in_at = timezone.now()
        appt.save(update_fields=["status", "checked_in_at", "updated_at"])
        aid = appt.id

        def queue_doctor_alert():
            from apps.notifications.tasks import notify_provider_patient_checked_in_task

            notify_provider_patient_checked_in_task.delay(aid)

        def queue_in_app():
            from apps.clinic.in_app_notify import create_checkin_in_app_notification

            create_checkin_in_app_notification(aid)

        transaction.on_commit(queue_doctor_alert)
        transaction.on_commit(queue_in_app)
        return Response({"detail": "Checked in", "status": appt.status})
