# Safe alignment of SystemErrorLog index names with Django's auto-generated names.
# Production already renamed these once; this migration is idempotent (renames only if old names exist).

from django.db import migrations, models


OLD_RESOLVED = "syserr_resolved_created_idx"
NEW_RESOLVED = "clinic_syst_resolve_bd6693_idx"
OLD_SOURCE = "syserr_source_created_idx"
NEW_SOURCE = "clinic_syst_source_a6a647_idx"


def _rename_index_if_needed(schema_editor, old_name: str, new_name: str) -> None:
    connection = schema_editor.connection
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT 1
            FROM pg_indexes
            WHERE schemaname = current_schema()
              AND indexname = %s
            """,
            [old_name],
        )
        if not cursor.fetchone():
            return
        cursor.execute(
            """
            SELECT 1
            FROM pg_indexes
            WHERE schemaname = current_schema()
              AND indexname = %s
            """,
            [new_name],
        )
        if cursor.fetchone():
            # New name already exists; drop the old leftover if present.
            cursor.execute(f'DROP INDEX IF EXISTS "{old_name}"')
            return
        cursor.execute(f'ALTER INDEX "{old_name}" RENAME TO "{new_name}"')


def forwards(apps, schema_editor):
    _rename_index_if_needed(schema_editor, OLD_RESOLVED, NEW_RESOLVED)
    _rename_index_if_needed(schema_editor, OLD_SOURCE, NEW_SOURCE)


def backwards(apps, schema_editor):
    _rename_index_if_needed(schema_editor, NEW_RESOLVED, OLD_RESOLVED)
    _rename_index_if_needed(schema_editor, NEW_SOURCE, OLD_SOURCE)


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0055_insurance_company_catalog"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(forwards, backwards),
            ],
            state_operations=[
                migrations.RemoveIndex(
                    model_name="systemerrorlog",
                    name=OLD_RESOLVED,
                ),
                migrations.RemoveIndex(
                    model_name="systemerrorlog",
                    name=OLD_SOURCE,
                ),
                migrations.AddIndex(
                    model_name="systemerrorlog",
                    index=models.Index(
                        fields=["resolved_at", "created_at"],
                        name=NEW_RESOLVED,
                    ),
                ),
                migrations.AddIndex(
                    model_name="systemerrorlog",
                    index=models.Index(
                        fields=["source", "created_at"],
                        name=NEW_SOURCE,
                    ),
                ),
            ],
        ),
    ]
