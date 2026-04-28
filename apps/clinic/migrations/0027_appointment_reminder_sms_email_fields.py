from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0026_patient_phone_allow_shared_household"),
    ]

    operations = [
        migrations.RenameField(
            model_name="appointment",
            old_name="sms_reminder_sent_at",
            new_name="day_before_reminder_sms_at",
        ),
        migrations.AddField(
            model_name="appointment",
            name="day_before_reminder_email_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="appointment",
            name="same_day_reminder_email_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="appointment",
            name="same_day_reminder_sms_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
