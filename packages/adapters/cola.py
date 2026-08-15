"""Cola de trabajo y dedupe, sobre Postgres.

Postgres en vez de Redis porque la instancia ya existe en el proyecto y el lock
por conversación tiene que vivir aquí de todas formas.

El lock es la parte que importa. Si un usuario manda dos mensajes seguidos
—cosa constante en WhatsApp: "quiero 200 unidades" y acto seguido "del modelo
cálido"— dos réplicas del worker los toman en paralelo, ambas cargan el mismo
historial y ambas escriben. El resultado es contexto perdido o dos propuestas
para el mismo pedido. `pg_try_advisory_xact_lock` sobre una clave derivada del
remitente serializa el procesamiento por conversación sin serializar todo.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import asyncpg


def lock_key(wa_id_hash: str) -> int:
    """Deriva la clave de advisory lock desde el hash del remitente.

    Postgres usa un bigint con signo, así que se toman 63 bits del digest.
    """
    digest = hashlib.blake2b(wa_id_hash.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") & 0x7FFF_FFFF_FFFF_FFFF


@dataclass(frozen=True, slots=True)
class Tarea:
    id: int
    wa_message_id: str
    payload: dict[str, Any]
    intentos: int


class ColaRepo:
    """Acceso a la cola. Toda escritura pasa por aquí."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def encolar(
        self, wa_message_id: str, wa_id_hash: str, payload: dict[str, Any]
    ) -> bool:
        """Registra el mensaje y lo encola, en una sola transacción.

        Returns:
            True si se encoló. False si ya estaba procesado — es un reintento de
            Meta o una entrega duplicada, y volver a encolarlo correría el
            agente dos veces sobre el mismo mensaje.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            insertado = await conn.fetchval(
                """
                INSERT INTO ventupilot.mensajes_procesados (wa_message_id)
                VALUES ($1)
                ON CONFLICT (wa_message_id) DO NOTHING
                RETURNING wa_message_id
                """,
                wa_message_id,
            )
            if insertado is None:
                return False

            await conn.execute(
                """
                INSERT INTO ventupilot.cola (wa_message_id, lock_key, payload)
                VALUES ($1, $2, $3::jsonb)
                """,
                wa_message_id,
                lock_key(wa_id_hash),
                json.dumps(payload),
            )
            return True

    async def tomar(self, conn: asyncpg.Connection) -> Tarea | None:
        """Reclama una tarea cuya conversación no esté siendo procesada.

        Debe llamarse dentro de una transacción: el advisory lock es de ámbito
        transaccional y se libera solo al hacer commit o rollback, lo que
        garantiza que se suelta incluso si el worker revienta a mitad.

        `SKIP LOCKED` evita que las réplicas se bloqueen entre sí, y el filtro
        por `pg_try_advisory_xact_lock` hace que una tarea cuya conversación ya
        está tomada se salte en vez de esperar.
        """
        fila = await conn.fetchrow(
            """
            UPDATE ventupilot.cola
            SET estado = 'en_proceso',
                intentos = intentos + 1,
                actualizada_at = now()
            WHERE id = (
                SELECT id FROM ventupilot.cola
                WHERE estado = 'pendiente'
                  AND disponible_at <= now()
                  AND pg_try_advisory_xact_lock(lock_key)
                ORDER BY disponible_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING id, wa_message_id, payload, intentos
            """
        )
        if fila is None:
            return None
        return Tarea(
            id=fila["id"],
            wa_message_id=fila["wa_message_id"],
            payload=json.loads(fila["payload"]),
            intentos=fila["intentos"],
        )

    async def completar(self, conn: asyncpg.Connection, tarea_id: int) -> None:
        await conn.execute(
            """
            UPDATE ventupilot.cola
            SET estado = 'completada', actualizada_at = now()
            WHERE id = $1
            """,
            tarea_id,
        )

    async def fallar(
        self,
        conn: asyncpg.Connection,
        tarea_id: int,
        error: str,
        max_intentos: int = 3,
        backoff_segundos: int = 30,
    ) -> None:
        """Devuelve la tarea a la cola con backoff, o la marca fallida.

        El mensaje de error se trunca: puede venir de un servicio externo y no
        tiene por qué caber ni ser confiable.
        """
        await conn.execute(
            """
            UPDATE ventupilot.cola
            SET estado = CASE
                    WHEN intentos >= $3 THEN 'fallida'::ventupilot.estado_tarea
                    ELSE 'pendiente'::ventupilot.estado_tarea
                END,
                disponible_at = now() + ($4 * intentos) * INTERVAL '1 second',
                ultimo_error = left($2, 2000),
                actualizada_at = now()
            WHERE id = $1
            """,
            tarea_id,
            error,
            max_intentos,
            backoff_segundos,
        )
