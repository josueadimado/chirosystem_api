# Manual override for patient "date established" (admin can set when different from first appointment).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0036_patient_marital_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="patient",
            name="date_established",
            field=models.DateField(
                blank=True,
                null=True,
                help_text="When set by staff, shown as date established instead of the first non-cancelled appointment date.",
            ),
        ),
    ]
