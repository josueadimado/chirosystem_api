# Generated manually for SystemErrorLog

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0048_patient_sms_consent_default_true"),
    ]

    operations = [
        migrations.CreateModel(
            name="SystemErrorLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "level",
                    models.CharField(
                        choices=[("error", "Error"), ("warning", "Warning"), ("critical", "Critical")],
                        default="error",
                        max_length=20,
                    ),
                ),
                (
                    "source",
                    models.CharField(
                        choices=[
                            ("api", "API"),
                            ("middleware", "Middleware"),
                            ("celery", "Celery"),
                            ("client", "Browser (admin)"),
                        ],
                        default="api",
                        max_length=20,
                    ),
                ),
                ("message", models.TextField()),
                ("exception_type", models.CharField(blank=True, default="", max_length=200)),
                ("traceback_text", models.TextField(blank=True, default="")),
                ("http_method", models.CharField(blank=True, default="", max_length=10)),
                ("path", models.CharField(blank=True, default="", max_length=500)),
                ("query_string", models.CharField(blank=True, default="", max_length=1000)),
                ("status_code", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("user_id", models.IntegerField(blank=True, null=True)),
                ("user_role", models.CharField(blank=True, default="", max_length=30)),
                ("user_display", models.CharField(blank=True, default="", max_length=200)),
                ("request_body", models.TextField(blank=True, default="")),
                ("extra", models.JSONField(blank=True, default=dict)),
                ("fingerprint", models.CharField(blank=True, db_index=True, default="", max_length=64)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                ("resolved_by_id", models.IntegerField(blank=True, null=True)),
                ("resolution_notes", models.TextField(blank=True, default="")),
            ],
            options={
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(fields=["resolved_at", "created_at"], name="syserr_resolved_created_idx"),
                    models.Index(fields=["source", "created_at"], name="syserr_source_created_idx"),
                ],
            },
        ),
    ]
