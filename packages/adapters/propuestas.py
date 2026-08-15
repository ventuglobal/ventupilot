"""Persistencia de propuestas, sobre `ventupilot.propuestas`.

Guardar la propuesta antes de enviarla es lo que hace posible la
confirmación: cuando el usuario pulsa el botón, lo único que llega es el id
del botón. Ese id es el `propuesta_id`, y con él se recupera la cotización
exacta que se mostró —con los precios de aquel momento— en vez de
reconstruirla y arriesgar que hayan cambiado.

Eso es el `snapshot` de la tabla: las líneas valorizadas, congeladas. La
columna `total` es NUMERIC(14,2) porque el esquema se pensó multi-moneda;
ventupilot trabaja en CLP entero y convierte en la frontera.
"""

from __future__ import annotations

import json
from decimal import Decimal

import asyncpg

from packages.domain.propuesta import EstadoPropuesta, LineaPropuesta, Propuesta


class PropuestasRepo:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def guardar(
        self, conn: asyncpg.Connection, propuesta: Propuesta, *, conversacion_id: int
    ) -> bool:
        """Persiste una propuesta. Devuelve False si ya existía.

        Recibe la conexión y no la toma del pool: se escribe dentro de la
        transacción del worker, junto con el encolado en el outbox. Si el
        proceso muere entre ambas cosas, no queda una propuesta guardada cuyo
        mensaje nunca salió.

        El id es determinístico, así que reprocesar el mismo mensaje reinserta
        la misma fila y el `DO NOTHING` la descarta (invariante 5). El False
        significa "este mensaje ya se procesó": no hay que reenviar nada.
        """
        resultado: str = await conn.execute(
            """
            INSERT INTO ventupilot.propuestas
                   (id, conversacion_id, wa_id_hash, snapshot, total, moneda,
                    estado, expira_at, creada_at)
            VALUES ($1, $2, $3, $4::jsonb, $5, 'CLP', $6, $7, $8)
            ON CONFLICT (id) DO NOTHING
            """,
            propuesta.propuesta_id,
            conversacion_id,
            propuesta.wa_id_hash,
            json.dumps([ln.model_dump() for ln in propuesta.lineas]),
            Decimal(propuesta.total_clp),
            propuesta.estado.value,
            propuesta.expira_at,
            propuesta.creada_at,
        )
        return resultado.endswith(" 1")

    async def obtener(self, propuesta_id: str) -> Propuesta | None:
        async with self._pool.acquire() as conn:
            fila = await conn.fetchrow(
                """
                SELECT id, wa_id_hash, snapshot, total, estado, creada_at, expira_at
                FROM ventupilot.propuestas
                WHERE id = $1
                """,
                propuesta_id,
            )

        if fila is None:
            return None

        return Propuesta(
            propuesta_id=fila["id"],
            wa_id_hash=fila["wa_id_hash"],
            lineas=[LineaPropuesta.model_validate(x) for x in json.loads(fila["snapshot"])],
            total_clp=int(fila["total"]),
            estado=EstadoPropuesta(fila["estado"]),
            creada_at=fila["creada_at"],
            expira_at=fila["expira_at"],
        )

    async def resolver(
        self,
        conn: asyncpg.Connection,
        propuesta_id: str,
        estado: EstadoPropuesta,
        *,
        wa_id_hash: str,
    ) -> bool:
        """Confirma o rechaza una propuesta. Devuelve si realmente cambió.

        Tres comprobaciones van en el propio WHERE, no en Python, para que la
        transición sea atómica y no haya ventana entre leer y escribir:

        - que siga vigente, para que una confirmación repetida no cuente dos
          veces;
        - que no haya expirado, porque un precio vencido no se ejecuta;
        - que el `wa_id_hash` coincida, para que nadie confirme la propuesta
          de otro conociendo su id.

        El False es genuinamente ambiguo entre los tres casos, y es correcto
        que lo sea: se responde "esa cotización ya no está disponible" sin
        revelar cuál de los tres motivos fue.
        """
        resultado: str = await conn.execute(
            """
            UPDATE ventupilot.propuestas
            SET estado = $2
            WHERE id = $1
              AND wa_id_hash = $3
              AND estado = 'vigente'
              AND expira_at > now()
            """,
            propuesta_id,
            estado.value,
            wa_id_hash,
        )
        return resultado.endswith(" 1")
