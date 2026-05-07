from decimal import Decimal

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0029_invoice_professional_discount_reason"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="patient",
            name="credit_balance",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("0"),
                help_text="In-house prepaid credit available to apply to future invoices.",
                max_digits=10,
            ),
        ),
        migrations.AddField(
            model_name="invoice",
            name="credit_applied_total",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("0"),
                help_text="Total in-house patient credit applied to this invoice over time.",
                max_digits=10,
            ),
        ),
        migrations.CreateModel(
            name="PatientCreditTransaction",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("kind", models.CharField(choices=[("top_up", "Top up"), ("apply_to_invoice", "Applied to invoice"), ("adjustment", "Manual adjustment")], max_length=30)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=10)),
                ("balance_after", models.DecimalField(decimal_places=2, max_digits=10)),
                ("note", models.CharField(blank=True, default="", max_length=300)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=models.deletion.SET_NULL, related_name="created_credit_transactions", to=settings.AUTH_USER_MODEL)),
                ("invoice", models.ForeignKey(blank=True, null=True, on_delete=models.deletion.SET_NULL, related_name="credit_transactions", to="clinic.invoice")),
                ("patient", models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="credit_transactions", to="clinic.patient")),
            ],
        ),
    ]
