"""Ejecución del pedido: crea la orden al confirmar una propuesta.

Dos escrituras, una sola transacción (la que ya tiene el worker abierta con
el advisory lock de la conversación):

- `ventupilot.ordenes`, la última defensa contra duplicados (invariante 5):
  el `idempotency_key` es el `propuesta_id`, así que confirmar la misma
  propuesta dos veces no crea una segunda orden. Se inserta primero y a
  propósito: si el `ON CONFLICT` la descarta, no se toca `orders_order` en
  absoluto — evita el caso en que un reintento cree una orden real huérfana
  aunque `ventupilot.ordenes` ya la tuviera registrada.
- `orders_order` / `orders_orderitem` de ventu 1.0, por SQL directo. Ambas
  Postgres son la misma instancia (ver README), así que esto es un INSERT
  más dentro de la misma transacción, no una llamada de red que pueda quedar
  a medias.

`orders_order`/`orders_orderitem` las gestiona Django, no esta migración:
las mantiene `ventu/orders/models.py`. El INSERT de abajo lista explícitamente
todas las columnas NOT NULL sin default a nivel de base de datos —Django no
empuja `default=` al esquema— para no depender de en qué orden se despliegan
ventupilot y ventu 1.0. Una columna NOT NULL nueva que se agregue ahí y no
esté aquí rompe este INSERT: es el costo aceptado de escribir SQL directo en
vez de pasar por un endpoint propio de ventu 1.0.

`source="whatsapp"` ya es un valor válido en `Order.SOURCE_CHOICES`, pero
`choices` no es una restricción de la base de datos: este INSERT no depende
de que esa migración de ventu ya esté desplegada.

Todo lo que ventu 1.0 no puede saber todavía —RUT, dirección de despacho— se
deja vacío. Es lo que hoy hace el ejecutivo a mano cuando confirma un pedido
por otro canal; acá no cambia quién lo hace, solo se le ahorra tipear de
nuevo el carrito.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import asyncpg

from packages.domain.propuesta import LineaPropuesta


class OrdenesRepo:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def crear(
        self,
        conn: asyncpg.Connection,
        propuesta_id: str,
        *,
        wa_id_hash: str,
        wa_id: str,
        nombre_perfil: str | None,
    ) -> int | None:
        """Crea la orden a partir de una propuesta ya confirmada.

        Devuelve el `id` de `orders_order` si se creó, o `None` si esta
        propuesta ya tenía una orden (invariante 5: no se reintenta).
        """
        fila = await conn.fetchrow(
            """
            SELECT snapshot, total
            FROM ventupilot.propuestas
            WHERE id = $1 AND wa_id_hash = $2
            """,
            propuesta_id,
            wa_id_hash,
        )
        if fila is None:
            # No debería pasar: `resolver()` ya validó dueño y vigencia antes
            # de llamar acá. Si pasa, es un bug en el llamador, no un estado
            # de negocio legítimo — no se inventa una orden vacía.
            return None

        lineas = [LineaPropuesta.model_validate(x) for x in json.loads(fila["snapshot"])]
        total = Decimal(fila["total"])

        orden_id = await conn.fetchval(
            """
            INSERT INTO ventupilot.ordenes (propuesta_id, idempotency_key)
            VALUES ($1, $1)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id
            """,
            propuesta_id,
        )
        if orden_id is None:
            return None

        ahora = datetime.now(tz=UTC)
        nota = (
            f"Pedido confirmado por WhatsApp ({wa_id}). "
            f"Propuesta {propuesta_id}, orden ventupilot #{orden_id}. "
            "Falta RUT y dirección de despacho: pendiente de contacto del "
            "ejecutivo antes de poder facturar."
        )

        # `orders_order` tiene ~30 columnas TEXT/CHAR con `blank=True` pero sin
        # `null=True` ni `default=`: a nivel de Django eso se rellena solo
        # porque `CharField.get_default()` cae a `""` cuando el ORM instancia
        # una fila nueva — un comportamiento de Python, no un DEFAULT de la
        # base. Un INSERT que las omita revienta con NotNullViolationError
        # (así se descubrió: contra una Postgres de prueba con este mismo
        # esquema). Van todas explícitas acá, aunque ventu 1.0 nunca las use
        # para una orden de WhatsApp, salvo `''` como valor.
        order_pk = await conn.fetchval(
            """
            INSERT INTO orders_order
                   (source, buyer_name, status, total_amount,
                    date_created, last_updated, created_at, updated_at,
                    discount_amount, shipping_cost,
                    shopify_order_name, falabella_order_number,
                    shipping_status, shipping_substatus,
                    shipping_logistic_type, shipping_mode,
                    shipping_tracking_number, is_flex, shipping_place_id,
                    shipping_receiver_address, shipping_receiver_name,
                    shipping_receiver_phone,
                    buyer_rut, buyer_rut_raw, buyer_cust_type, buyer_giro,
                    buyer_address, buyer_commune, buyer_city,
                    billing_is_normalized, billing_placeholders,
                    billing_fetch_status, billing_error_msg,
                    ventu_notes, ventu_ok, ventu_archived, has_open_claim,
                    supplier_group)
            VALUES ($1, $2, $3, $4,
                    $5, $5, $5, $5,
                    0, 0,
                    $7::text, $7::text,
                    $7::text, $7::text,
                    $7::text, $7::text,
                    $7::text, false, $7::text,
                    $7::text, $7::text,
                    $7::text,
                    $7::text, $7::text, $7::text, $7::text,
                    $7::text, $7::text, $7::text,
                    false, '[]'::jsonb,
                    $7::text, $7::text,
                    $6, false, false, false,
                    $7::text)
            RETURNING id
            """,
            "whatsapp",
            (nombre_perfil or "")[:150],
            "pendiente",
            total,
            ahora,
            nota,
            "",
        )

        await conn.executemany(
            """
            INSERT INTO orders_orderitem
                   (order_id, ml_item_id, ml_variation_id, sku, title,
                    quantity, price, falabella_item_ids, supplier_order_ref)
            VALUES ($1, '', '', $2, $3, $4, $5, '[]'::jsonb, '')
            """,
            [
                (order_pk, ln.sku, ln.titulo, ln.cantidad, Decimal(ln.precio_unitario_clp))
                for ln in lineas
            ],
        )

        await conn.execute(
            "UPDATE ventupilot.ordenes SET referencia_ext = $1 WHERE id = $2",
            str(order_pk),
            orden_id,
        )

        return int(order_pk)
