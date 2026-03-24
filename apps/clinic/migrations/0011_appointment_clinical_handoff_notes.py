from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0010_service_public_booking_and_code_length"),
    ]

    operations = [
        migrations.AddField(
            model_name="appointment",
            name="clinical_handoff_notes",
            field=models.TextField(
                blank=True,
                default="",
                help_text="Clinical or admin notes for future visits—visible to other doctors on this patient's chart.",
            ),
        ),
    ]
