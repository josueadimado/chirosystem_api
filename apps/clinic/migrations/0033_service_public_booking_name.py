from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0032_clinicsettings_employer_tax_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="service",
            name="public_booking_name",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Optional. When set, patients see this on the public booking site, voice booking, and in confirmations "
                "instead of Name. Schedules, doctor portal, and billing always use Name.",
                max_length=200,
            ),
        ),
    ]
