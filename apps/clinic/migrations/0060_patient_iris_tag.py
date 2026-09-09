# Generated manually for Iris referral / patient tag

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0059_patient_saved_card"),
    ]

    operations = [
        migrations.AddField(
            model_name="patient",
            name="iris_tag",
            field=models.BooleanField(
                db_index=True,
                default=False,
                help_text="True when this patient is referred by Iris or is an Iris (nutrition) patient. Can combine with cash or insurance.",
            ),
        ),
        migrations.AddField(
            model_name="patient",
            name="iris_tagged_at",
            field=models.DateTimeField(
                blank=True,
                help_text="When iris_tag was last turned on (for analytics).",
                null=True,
            ),
        ),
    ]
