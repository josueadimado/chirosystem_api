from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0044_add_db_indexes"),
    ]

    operations = [
        migrations.AddField(
            model_name="patient",
            name="payment_profile",
            field=models.CharField(
                blank=True,
                choices=[
                    ("", "Not set"),
                    ("insurance", "Insurance"),
                    ("cash", "Cash / self-pay"),
                ],
                default="",
                help_text="Shown on the schedule: insurance (eye icon) or cash. Set by staff during a visit.",
                max_length=20,
            ),
        ),
    ]
