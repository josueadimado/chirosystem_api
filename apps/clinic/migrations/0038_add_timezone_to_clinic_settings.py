from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0037_patient_date_established"),
    ]

    operations = [
        migrations.AddField(
            model_name="clinicsettings",
            name="timezone",
            field=models.CharField(
                default="America/Detroit",
                help_text=(
                    "IANA timezone for the clinic. "
                    "Controls scheduling, AI voice, "
                    "and appointment times. "
                    "Example: America/Detroit, "
                    "America/Chicago, America/New_York"
                ),
                max_length=64,
            ),
        ),
    ]
