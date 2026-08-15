"""Outbox de mensajes salientes.

El worker no envía a Graph API dentro de su transacción: escribe aquí y hace
commit junto con el resto del trabajo. Un despachador aparte lee y envía.

El motivo es que sin outbox hay una ventana donde la tool ya se ejecutó —la
orden existe— pero el POST a Graph API falló, y entonces lo que el sistema sabe
y lo que el cliente sabe divergen sin que nadie se entere. Con outbox, o se
persiste todo o no se persiste nada, y el envío es reintentable.

La `idempotency_key` es determinística: reintentar tras un fallo ambiguo no le
manda al cliente el mismo mensaje dos veces.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import asyncpg


@dataclass(frozen=True, slots=True)
class Saliente:
    id: int
    cuerpo: dict[str, Any]
    intentos: int


async def encolar_salida(
    conn: asyncpg.Connection,
    *,
    wa_id_hash: str,
    idempotency_key: str,
    cuerpo: dict[str, Any],
    conversacion_id: int | None = None,
) -> bool:
    """Registra un mensaje para envío. Debe correr en la transacción del worker.

    Returns:
        False si la clave ya existía — el mensaje ya se registró antes y volver
        a encolarlo se lo mandaría dos veces al cliente.
    """
    fila = await conn.fetchval(
        """
        INSERT INTO ventupilot.mensajes_salientes
            (conversacion_id, wa_id_hash, idempotency_key, cuerpo)
        VALUES ($1, $2, $3, $4::jsonb)
        ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING id
        """,
        conversacion_id,
        wa_id_hash,
        idempotency_key,
        json.dumps(cuerpo),
    )
    return fila is not None


async def tomar_salida(conn: asyncpg.Connection) -> Saliente | None:
    """Reclama un mensaje pendiente de envío.

    Debe correr dentro de una transacción: `FOR UPDATE SKIP LOCKED` mantiene la
    fila reservada hasta el commit, de modo que dos despachadores no envían el
    mismo mensaje.
    """
    fila = await conn.fetchrow(
        """
        UPDATE ventupilot.mensajes_salientes
        SET estado = 'enviando', intentos = intentos + 1
        WHERE id = (
            SELECT id FROM ventupilot.mensajes_salientes
            WHERE estado = 'pendiente' AND disponible_at <= now()
            ORDER BY disponible_at
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING id, cuerpo, intentos
        """
    )
    if fila is None:
        return None
    return Saliente(
        id=fila["id"], cuerpo=json.loads(fila["cuerpo"]), intentos=fila["intentos"]
    )


async def marcar_enviado(
    conn: asyncpg.Connection, saliente_id: int, wa_message_id: str
) -> None:
    await conn.execute(
        """
        UPDATE ventupilot.mensajes_salientes
        SET estado = 'enviado', wa_message_id = $2, ultimo_error = NULL
        WHERE id = $1
        """,
        saliente_id,
        wa_message_id,
    )


async def marcar_fallo(
    conn: asyncpg.Connection,
    saliente_id: int,
    error: str,
    *,
    reintentable: bool,
    max_intentos: int = 4,
    backoff_segundos: int = 20,
) -> None:
    """Devuelve el mensaje a la cola con backoff, o lo descarta.

    Un error no reintentable —ventana de 24h cerrada, número inválido— se
    descarta de inmediato: insistir gasta cuota y degrada el quality rating.
    """
    await conn.execute(
        """
        UPDATE ventupilot.mensajes_salientes
        SET estado = CASE
                WHEN NOT $3 OR intentos >= $4 THEN 'descartado'
                ELSE 'pendiente'
            END,
            disponible_at = now() + ($5 * intentos) * INTERVAL '1 second',
            ultimo_error = left($2, 2000)
        WHERE id = $1
        """,
        saliente_id,
        error,
        reintentable,
        max_intentos,
        backoff_segundos,
    )
