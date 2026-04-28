from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0025_patient_online_chiro_intake_waived"),
    ]

    operations = [
        migrations.AlterField(
            model_name="patient",
            name="phone",
            field=models.CharField(db_index=True, max_length=20),
        ),
    ]
