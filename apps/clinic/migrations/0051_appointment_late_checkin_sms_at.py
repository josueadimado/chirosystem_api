from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("clinic", "0050_clinicsettings_error_tracker_password"),
    ]

    operations = [
        migrations.AddField(
            model_name="appointment",
            name="late_checkin_sms_at",
            field=models.DateTimeField(
                blank=True,
                help_text="Set when the patient was texted because they had not checked in after the scheduled start.",
                null=True,
            ),
        ),
    ]
