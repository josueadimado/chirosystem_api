# Generated manually — auto no-show after grace period

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0041_patient_communication_prefs"),
    ]

    operations = [
        migrations.AddField(
            model_name="appointment",
            name="auto_no_show_processed_at",
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text="Set when the system automatically marked this visit as no-show (grace period after start).",
            ),
        ),
        migrations.AddField(
            model_name="appointment",
            name="no_show_notice_sms_at",
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text="When a no-show billing notice SMS was sent to the patient.",
            ),
        ),
        migrations.AddField(
            model_name="appointment",
            name="no_show_notice_email_at",
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text="When a no-show billing notice email was sent to the patient.",
            ),
        ),
        migrations.AddField(
            model_name="clinicsettings",
            name="auto_no_show_enabled",
            field=models.BooleanField(
                default=True,
                help_text="When enabled, booked/checked-in visits are marked no-show automatically after the grace period.",
            ),
        ),
        migrations.AddField(
            model_name="clinicsettings",
            name="auto_no_show_grace_minutes",
            field=models.PositiveSmallIntegerField(
                default=60,
                help_text="Minutes after the scheduled start before an unattended visit becomes an automatic no-show.",
            ),
        ),
    ]
