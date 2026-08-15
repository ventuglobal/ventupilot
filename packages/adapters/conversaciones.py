"""Conversaciones e historial.

`migrations/001_inicial.sql` ya define ambas tablas; hasta la Fase 2 nadie
las poblaba porque el handler de eco no necesitaba contexto.

Dos cosas dependen de esto:

- `ventupilot.propuestas.conversacion_id` es NOT NULL, así que no hay
  cotización sin conversación.
- `ultimo_msg_usuario_at` determina si estamos dentro de la ventana de 24h
  de Meta. Fuera de ella un mensaje libre se rechaza y hace falta plantilla,
  así que el dato tiene que estar al día aunque todavía no lo usemos para
  decidir.

El historial se guarda como mensajes serializados de pydantic-ai, no como
texto plano: es lo que permite devolvérselos al modelo en el turno siguiente
sin perder tool calls ni resultados.
"""

from __future__ import annotations

import json

import asyncpg


async def obtener_o_crear(
    conn: asyncpg.Connection, wa_id_hash: str, phone_number_id: str
) -> int:
    """Devuelve el id de la conversación activa, creándola si hace falta.

    El índice único parcial `ux_conversacion_activa` garantiza que solo haya
    una activa por (remitente, número); el `ON CONFLICT` sobre él convierte
    la carrera entre dos réplicas en un no-op en vez de en una excepción.

    Debe correr dentro de la transacción del worker: la conversación y lo que
    se escriba a continuación tienen que aparecer juntas o no aparecer.
    """
    fila = await conn.fetchrow(
        """
        INSERT INTO ventupilot.conversaciones (wa_id_hash, phone_number_id)
        VALUES ($1, $2)
        ON CONFLICT (wa_id_hash, phone_number_id) WHERE estado = 'activa'
        DO UPDATE SET actualizada_at = now()
        RETURNING id
        """,
        wa_id_hash,
        phone_number_id,
    )
    return int(fila["id"])


async def marcar_mensaje_usuario(conn: asyncpg.Connection, conversacion_id: int) -> None:
    """Actualiza la marca de la ventana de 24h."""
    await conn.execute(
        """
        UPDATE ventupilot.conversaciones
        SET ultimo_msg_usuario_at = now(), actualizada_at = now()
        WHERE id = $1
        """,
        conversacion_id,
    )


async def cargar_historial(
    conn: asyncpg.Connection, conversacion_id: int, *, limite: int
) -> list[dict[str, object]]:
    """Devuelve los últimos mensajes, en orden cronológico.

    Se limita por número de mensajes y no por tokens porque el tope real lo
    pone `max_requests_per_run`; esto solo evita que una conversación de
    meses entre entera en el prompt. Se piden los últimos y luego se
    invierten: sin `ORDER BY seq DESC` el LIMIT recortaría por el principio.
    """
    filas = await conn.fetch(
        """
        SELECT payload
        FROM ventupilot.historial
        WHERE conversacion_id = $1
        ORDER BY seq DESC
        LIMIT $2
        """,
        conversacion_id,
        limite,
    )
    return [json.loads(f["payload"]) for f in reversed(filas)]


async def anexar_historial(
    conn: asyncpg.Connection, conversacion_id: int, mensajes: list[dict[str, object]]
) -> None:
    """Añade mensajes al historial.

    El `seq` se calcula dentro del INSERT y no en Python: dos réplicas que
    escriban a la vez se serializan por la primary key compuesta en vez de
    pisarse. Es la misma razón por la que esto corre en la transacción del
    worker, que además tiene tomado el advisory lock de la conversación.
    """
    if not mensajes:
        return

    await conn.executemany(
        """
        INSERT INTO ventupilot.historial (conversacion_id, seq, payload)
        VALUES (
            $1,
            (SELECT COALESCE(max(seq), 0) + 1
               FROM ventupilot.historial
              WHERE conversacion_id = $1),
            $2::jsonb
        )
        """,
        [(conversacion_id, json.dumps(m, default=str)) for m in mensajes],
    )
