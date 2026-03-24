"""
Repair clinic_patient when Square columns are missing (e.g. DB restored from backup, or renames never ran).

Renames legacy DB columns if present; otherwise adds square_* fields. Safe to run multiple times.
"""

from django.db import migrations


def ensure_square_columns(apps, schema_editor):
    conn = schema_editor.connection
    if conn.vendor != "postgresql":
        return

    def col_exists(cursor, name: str) -> bool:
        cursor.execute(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = 'clinic_patient'
              AND column_name = %s
            """,
            [name],
        )
        return cursor.fetchone() is not None

    # Old DBs may still have these physical column names from migration 0004; Square is the only processor now.
    with conn.cursor() as cursor:
        if col_exists(cursor, "stripe_customer_id") and not col_exists(cursor, "square_customer_id"):
            cursor.execute(
                'ALTER TABLE clinic_patient RENAME COLUMN stripe_customer_id TO square_customer_id'
            )
        if col_exists(cursor, "stripe_default_payment_method_id") and not col_exists(
            cursor, "square_card_id"
        ):
            cursor.execute(
                "ALTER TABLE clinic_patient RENAME COLUMN stripe_default_payment_method_id TO square_card_id"
            )
        if not col_exists(cursor, "square_customer_id"):
            cursor.execute(
                "ALTER TABLE clinic_patient ADD COLUMN square_customer_id varchar(255) NOT NULL DEFAULT ''"
            )
        if not col_exists(cursor, "square_card_id"):
            cursor.execute(
                "ALTER TABLE clinic_patient ADD COLUMN square_card_id varchar(255) NOT NULL DEFAULT ''"
            )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0015_sync_voicecalllog_indexes_and_provider_services"),
    ]

    operations = [
        migrations.RunPython(ensure_square_columns, noop_reverse),
    ]
