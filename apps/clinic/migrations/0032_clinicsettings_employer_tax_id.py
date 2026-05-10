from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0031_provider_credential"),
    ]

    operations = [
        migrations.AddField(
            model_name="clinicsettings",
            name="employer_tax_id",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Employer / office tax ID printed on patient bills (e.g. EIN). Optional.",
                max_length=32,
            ),
        ),
    ]
