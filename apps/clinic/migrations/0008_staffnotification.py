import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("clinic", "0007_provider_notification_phone"),
    ]

    operations = [
        migrations.CreateModel(
            name="StaffNotification",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("kind", models.CharField(choices=[("checkin", "Check-in"), ("new_booking", "New booking"), ("schedule_change", "Schedule change"), ("reassigned_away", "Reassigned away")], max_length=30)),
                ("message", models.TextField()),
                ("read_at", models.DateTimeField(blank=True, null=True)),
                (
                    "appointment",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="staff_notifications",
                        to="clinic.appointment",
                    ),
                ),
                (
                    "recipient",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="staff_notifications",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="staffnotification",
            index=models.Index(fields=["recipient", "created_at"], name="clinic_staf_recipie_7e8a0f_idx"),
        ),
        migrations.AddIndex(
            model_name="staffnotification",
            index=models.Index(fields=["recipient", "read_at"], name="clinic_staf_recipie_8f1b2c_idx"),
        ),
    ]
