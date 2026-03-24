import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0011_appointment_clinical_handoff_notes"),
    ]

    operations = [
        migrations.CreateModel(
            name="ProviderUnavailability",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("block_date", models.DateField(db_index=True)),
                ("all_day", models.BooleanField(default=True)),
                ("start_time", models.TimeField(blank=True, null=True)),
                ("end_time", models.TimeField(blank=True, null=True)),
                (
                    "provider",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="unavailability_blocks",
                        to="clinic.provider",
                    ),
                ),
            ],
            options={
                "verbose_name": "Provider online booking block",
                "verbose_name_plural": "Provider online booking blocks",
                "ordering": ["-block_date", "start_time"],
            },
        ),
    ]
