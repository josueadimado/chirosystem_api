from django.db import migrations, models
from django.utils import timezone


def enable_sms_consent_for_existing(apps, schema_editor):
    Patient = apps.get_model("clinic", "Patient")
    now = timezone.now()
    Patient.objects.filter(sms_consent=False).update(sms_consent=True)
    Patient.objects.filter(sms_consent=True, sms_consent_at__isnull=True).update(sms_consent_at=now)


class Migration(migrations.Migration):
    dependencies = [
        ("clinic", "0047_appointment_auto_no_show_exempt"),
    ]

    operations = [
        migrations.AlterField(
            model_name="patient",
            name="sms_consent",
            field=models.BooleanField(
                default=True,
                help_text="True when the patient may receive SMS appointment reminders. On by default; staff or doctor can turn off.",
            ),
        ),
        migrations.AlterField(
            model_name="patient",
            name="sms_consent_at",
            field=models.DateTimeField(
                blank=True,
                help_text="When SMS consent was last turned on (online booking or staff save).",
                null=True,
            ),
        ),
        migrations.RunPython(enable_sms_consent_for_existing, migrations.RunPython.noop),
    ]
