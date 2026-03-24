from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("clinic", "0016_ensure_patient_square_columns"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="provider",
            options={
                "verbose_name": "Doctor",
                "verbose_name_plural": "Doctors",
            },
        ),
    ]
