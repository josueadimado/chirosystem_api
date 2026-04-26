from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("clinic", "0023_invoice_kind_late_cancel_fee"),
    ]

    operations = [
        migrations.AddField(
            model_name="patient",
            name="sms_consent",
            field=models.BooleanField(
                default=False,
                help_text="True if the patient agreed to SMS appointment reminders via the online booking consent checkbox.",
            ),
        ),
        migrations.AddField(
            model_name="patient",
            name="sms_consent_at",
            field=models.DateTimeField(
                blank=True,
                help_text="When SMS consent was last recorded from the booking site.",
                null=True,
            ),
        ),
    ]
