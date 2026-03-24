from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("clinic", "0013_voicecalllog"),
    ]

    operations = [
        migrations.RenameField(
            model_name="patient",
            old_name="stripe_customer_id",
            new_name="square_customer_id",
        ),
        migrations.RenameField(
            model_name="patient",
            old_name="stripe_default_payment_method_id",
            new_name="square_card_id",
        ),
    ]
