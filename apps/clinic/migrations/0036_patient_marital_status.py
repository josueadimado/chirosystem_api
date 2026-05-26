from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0035_billing_provider_ids"),
    ]

    operations = [
        migrations.AddField(
            model_name="patient",
            name="marital_status",
            field=models.CharField(
                blank=True,
                choices=[("", "—"), ("Y", "Married"), ("N", "Not married")],
                help_text="Y = married, N = not married (matrimonial situation).",
                max_length=1,
            ),
        ),
    ]
