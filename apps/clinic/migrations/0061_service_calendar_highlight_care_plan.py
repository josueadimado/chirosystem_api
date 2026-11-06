# Generated manually for care-plan / 2nd-visit staff-only booking.

from django.db import migrations, models
from django.db.models import Q


def ensure_care_plan_service(apps, schema_editor):
    """Create the staff-only 30-min care plan visit and link chiropractic providers."""
    Service = apps.get_model("clinic", "Service")
    Provider = apps.get_model("clinic", "Provider")

    svc, _ = Service.objects.update_or_create(
        name="Care plan visit (2nd visit)",
        defaults={
            "description": (
                "Staff-only 30-minute visit for a new patient's second appointment — "
                "review the care/treatment plan and treat. Not offered on the public booking site."
            ),
            "billing_code": "98941",
            "duration_minutes": 30,
            "price": "55.00",
            "is_active": True,
            "show_in_public_booking": False,
            "calendar_highlight": True,
            "service_type": "chiropractic",
            "visible_to_chiropractic_staff": True,
            "visible_to_massage_staff": False,
            "is_new_client_intake": False,
            "charges_patient": True,
        },
    )

    chiro_providers = list(
        Provider.objects.filter(active=True, primary_service_type="chiropractic")
    )
    if not chiro_providers:
        chiro_providers = list(
            Provider.objects.filter(active=True).filter(
                Q(specialty__icontains="chiro") | Q(title__icontains="chiro")
            )
        )
    if chiro_providers:
        svc.providers.set(chiro_providers)


def noop_reverse(apps, schema_editor):
    # Keep the service row; staff may have booked appointments against it.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0060_patient_iris_tag"),
    ]

    operations = [
        migrations.AddField(
            model_name="service",
            name="calendar_highlight",
            field=models.BooleanField(
                default=False,
                help_text="If True, admin/doctor calendars show this visit type in a distinct teal color.",
            ),
        ),
        migrations.RunPython(ensure_care_plan_service, noop_reverse),
    ]
