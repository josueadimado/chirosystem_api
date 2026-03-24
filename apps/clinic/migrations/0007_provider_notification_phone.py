from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("clinic", "0006_provider_google_calendar_appointment_event_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="provider",
            name="notification_phone",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Doctor/staff mobile for alerts (E.164 e.g. +15551234567). Leave blank to skip.",
                max_length=20,
            ),
        ),
    ]
