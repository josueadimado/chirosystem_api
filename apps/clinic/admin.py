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

admin.site.register(Patient)
admin.site.register(Provider)
admin.site.register(ProviderUnavailability)
admin.site.register(Service)
admin.site.register(Appointment)
admin.site.register(Visit)
admin.site.register(VisitRenderedService)
admin.site.register(Invoice)
admin.site.register(Payment)
