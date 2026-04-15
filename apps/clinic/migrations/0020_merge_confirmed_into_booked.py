from django.db import migrations, models


def merge_confirmed_into_booked(apps, schema_editor):
    Appointment = apps.get_model("clinic", "Appointment")
    Appointment.objects.filter(status="confirmed").update(status="booked")


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0019_service_staff_visibility"),
    ]

    operations = [
        migrations.RunPython(merge_confirmed_into_booked, noop_reverse),
        migrations.AlterField(
            model_name="appointment",
            name="status",
            field=models.CharField(
                choices=[
                    ("booked", "Booked"),
                    ("checked_in", "Checked In"),
                    ("in_consultation", "In Consultation"),
                    ("awaiting_payment", "Awaiting Payment"),
                    ("completed", "Completed"),
                    ("cancelled", "Cancelled"),
                    ("no_show", "No Show"),
                ],
                default="booked",
                max_length=30,
            ),
        ),
    ]
