from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0030_patient_credit_wallet"),
    ]

    operations = [
        migrations.AddField(
            model_name="provider",
            name="credential",
            field=models.CharField(
                blank=True,
                help_text="Displayed on printed patient bills, e.g. DC, PT, LMT.",
                max_length=100,
            ),
        ),
    ]
