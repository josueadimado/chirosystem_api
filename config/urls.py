from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView

from apps.accounts.views import AuthViewSet, TeamViewSet
from apps.clinic.square_pos_callback import square_pos_callback
from apps.clinic.square_webhook import square_webhook
from apps.clinic.voice_views import twilio_voice_gather, twilio_voice_incoming
from apps.clinic.views import (
    AdminViewSet,
    AppointmentViewSet,
    BookingOptionsViewSet,
    DoctorViewSet,
    InvoiceViewSet,
    KioskViewSet,
    PatientViewSet,
    PaymentViewSet,
    ProviderUnavailabilityViewSet,
    ProviderViewSet,
    ServiceViewSet,
    StaffNotificationViewSet,
    VisitViewSet,
)

router = DefaultRouter()
router.register("auth", AuthViewSet, basename="auth")
router.register("team", TeamViewSet, basename="team")
router.register("booking-options", BookingOptionsViewSet, basename="booking-options")
router.register("doctor", DoctorViewSet, basename="doctor")
router.register("patients", PatientViewSet, basename="patients")
router.register("providers", ProviderViewSet, basename="providers")
router.register(
    "provider-unavailability",
    ProviderUnavailabilityViewSet,
    basename="provider-unavailability",
)
router.register("services", ServiceViewSet, basename="services")
router.register("appointments", AppointmentViewSet, basename="appointments")
router.register("visits", VisitViewSet, basename="visits")
router.register("invoices", InvoiceViewSet, basename="invoices")
router.register("payments", PaymentViewSet, basename="payments")
router.register("kiosk", KioskViewSet, basename="kiosk")
router.register("notifications", StaffNotificationViewSet, basename="notifications")
router.register("admin", AdminViewSet, basename="admin")

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/square/webhook/", square_webhook, name="square-webhook"),
    path("api/v1/square/pos-callback/", square_pos_callback, name="square-pos-callback"),
    path("api/v1/voice/twilio/incoming/", twilio_voice_incoming, name="twilio_voice_incoming"),
    path("api/v1/voice/twilio/gather/", twilio_voice_gather, name="twilio_voice_gather"),
    path("api/v1/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/v1/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="swagger-ui"),
    path("api/v1/auth/token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("api/v1/", include(router.urls)),
]
