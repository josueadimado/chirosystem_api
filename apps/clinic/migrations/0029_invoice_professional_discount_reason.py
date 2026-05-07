from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0028_service_visitrendered_charges_patient"),
    ]

    operations = [
        migrations.AddField(
            model_name="invoice",
            name="professional_discount_reason",
            field=models.TextField(
                blank=True,
                default="",
                help_text="Optional internal note for why a professional discount was applied. Not shown on patient-facing printed bills.",
            ),
        ),
    ]
