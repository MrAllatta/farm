from django.db import migrations


def _to_jsonb(schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'reference_saleschannel'
                  AND column_name = 'days_of_week'
                  AND udt_name = '_varchar'
            ) THEN
                ALTER TABLE reference_saleschannel
                ALTER COLUMN days_of_week TYPE jsonb
                USING to_jsonb(days_of_week);
            END IF;
        END $$;
        """
    )


def _to_varchar_array(schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'reference_saleschannel'
                  AND column_name = 'days_of_week'
                  AND udt_name = 'jsonb'
            ) THEN
                ALTER TABLE reference_saleschannel
                ALTER COLUMN days_of_week TYPE varchar(10)[]
                USING ARRAY(SELECT jsonb_array_elements_text(days_of_week));
            END IF;
        END $$;
        """
    )


class Migration(migrations.Migration):
    dependencies = [
        ("reference", "0008_cropsalesformat_year_and_price_cache"),
    ]

    operations = [
        migrations.RunPython(
            code=lambda apps, schema_editor: _to_jsonb(schema_editor),
            reverse_code=lambda apps, schema_editor: _to_varchar_array(schema_editor),
        )
    ]
