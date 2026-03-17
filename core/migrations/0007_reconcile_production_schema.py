from django.db import migrations, connection


def forwards(apps, schema_editor):
    if connection.vendor != "postgresql":
        # Se não for PostgreSQL (ex: SQLite), não executa nada
        return

    with connection.cursor() as cursor:
        cursor.execute("""
            ALTER TABLE core_pedido
                ADD COLUMN IF NOT EXISTS status_pagamento VARCHAR(20);
        """)

        cursor.execute("""
            ALTER TABLE core_pedido
                ADD COLUMN IF NOT EXISTS total_pago NUMERIC(10,2) NOT NULL DEFAULT 0;
        """)

        cursor.execute("""
            UPDATE core_pedido p
            SET total_pago = COALESCE((
                SELECT SUM(pp.valor)
                FROM core_pagamentopedido pp
                WHERE pp.pedido_id = p.id
            ), 0);
        """)

        cursor.execute("""
            UPDATE core_pedido
            SET status_pagamento = CASE
                WHEN COALESCE(total_pago, 0) >= COALESCE(total, 0) AND COALESCE(total, 0) > 0 THEN 'pago'
                WHEN COALESCE(total_pago, 0) > 0 THEN 'parcial'
                ELSE 'aberto'
            END;
        """)

        cursor.execute("""
            UPDATE core_pedido
            SET pago = TRUE
            WHERE status_pagamento = 'pago' AND pago = FALSE;
        """)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0006_alter_pedido_options_remove_pedido_metodo_pagamento_and_more"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]