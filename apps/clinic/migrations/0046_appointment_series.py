from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0045_patient_payment_profile"),
    ]

    operations = [
        migrations.CreateModel(
            name="AppointmentSeries",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("start_time", models.TimeField(help_text="Shared start time for each occurrence.")),
                (
                    "recurrence",
                    models.CharField(
                        choices=[
                            ("weekly", "Weekly"),
                            ("biweekly", "Every 2 weeks"),
                            ("monthly", "Monthly"),
                        ],
                        max_length=20,
                    ),
                ),
                ("first_appointment_date", models.DateField()),
                ("last_appointment_date", models.DateField()),
                ("occurrence_count", models.PositiveSmallIntegerField(default=1)),
                (
                    "booked_service",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to="clinic.service",
                    ),
                ),
                (
                    "patient",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="appointment_series",
                        to="clinic.patient",
                    ),
                ),
                (
                    "provider",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to="clinic.provider"),
                ),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddField(
            model_name="appointment",
            name="series",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="appointments",
                to="clinic.appointmentseries",
            ),
        ),
    ]
