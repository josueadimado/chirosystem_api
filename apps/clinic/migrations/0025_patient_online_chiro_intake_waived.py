from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("clinic", "0024_patient_sms_consent"),
    ]

    operations = [
        migrations.AddField(
            model_name="patient",
            name="online_chiro_intake_waived",
            field=models.BooleanField(
                default=False,
                help_text="If checked, this patient may book regular (non-intake) chiropractic online even without a completed chiropractic visit on file—use for data imports and established patients from before the system.",
            ),
        ),
    ]
