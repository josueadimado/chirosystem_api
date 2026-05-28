"""Legacy no-shows were stored as awaiting_payment when the no-show fee invoice was unpaid."""

from django.db import migrations


def fix_no_show_statuses(apps, schema_editor):
    Appointment = apps.get_model("clinic", "Appointment")
    Invoice = apps.get_model("clinic", "Invoice")
    for inv in Invoice.objects.filter(kind="no_show_fee").select_related("appointment"):
        appt = inv.appointment
        if appt.status == "awaiting_payment":
            appt.status = "no_show"
            appt.save(update_fields=["status", "updated_at"])


class Migration(migrations.Migration):
    dependencies = [
        ("clinic", "0039_diagnosis_catalog"),
    ]

    operations = [
        migrations.RunPython(fix_no_show_statuses, migrations.RunPython.noop),
    ]
