"""Registro de remitentes autorizados (invariante 4).

Se consulta por `wa_id_hash`: el número en claro no se almacena.

Este módulo no crea clientes desde el flujo de mensajes. Un desconocido que
escribe no se auto-registra ni hereda permisos por coincidir con algún
teléfono de `orders_customer` — eso convertiría un dato importado de
MercadoLibre en una credencial. Dar de alta a alguien es un acto deliberado
del operador; por eso `autorizar` está pensada para un comando de
mantenimiento y no para el camino caliente.
"""

from __future__ import annotations

import asyncpg

from packages.domain.identidad import DESCONOCIDO, ClienteAutorizado, Permiso

_VALIDOS = {p.value for p in Permiso}


class ClientesRepo:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def obtener(self, wa_id_hash: str) -> ClienteAutorizado:
        """Devuelve el cliente, o `DESCONOCIDO` si no está registrado.

        Devolver un objeto en vez de None es intencional: obliga a pasar por
        `puede()` y hace imposible el olvido de comprobar None y seguir
        adelante como si estuviera autorizado.
        """
        async with self._pool.acquire() as conn:
            fila = await conn.fetchrow(
                """
                SELECT wa_id_hash, customer_id, nombre, permisos, activo
                FROM ventupilot.clientes
                WHERE wa_id_hash = $1
                """,
                wa_id_hash,
            )

        if fila is None:
            return DESCONOCIDO

        # Un permiso que no reconocemos —uno de una versión anterior, por
        # ejemplo— se ignora en vez de reventar. Ignorar degrada a menos
        # permisos, que es el lado seguro.
        permisos = frozenset(Permiso(p) for p in (fila["permisos"] or []) if p in _VALIDOS)

        return ClienteAutorizado(
            wa_id_hash=fila["wa_id_hash"],
            customer_id=fila["customer_id"],
            nombre=fila["nombre"],
            permisos=permisos,
            activo=fila["activo"],
        )

    async def autorizar(
        self,
        wa_id_hash: str,
        permisos: set[Permiso],
        *,
        customer_id: int | None = None,
        nombre: str | None = None,
    ) -> None:
        """Da de alta o actualiza a un remitente. Uso administrativo.

        Idempotente por `wa_id_hash`. El `COALESCE` deja cambiar solo los
        permisos sin borrar el nombre o el `customer_id` ya puestos.
        """
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO ventupilot.clientes
                       (wa_id_hash, customer_id, nombre, permisos, activo)
                VALUES ($1, $2, $3, $4::text[], true)
                ON CONFLICT (wa_id_hash) DO UPDATE
                   SET permisos       = EXCLUDED.permisos,
                       customer_id    = COALESCE(EXCLUDED.customer_id,
                                                 ventupilot.clientes.customer_id),
                       nombre         = COALESCE(EXCLUDED.nombre,
                                                 ventupilot.clientes.nombre),
                       actualizado_at = now()
                """,
                wa_id_hash,
                customer_id,
                nombre,
                sorted(p.value for p in permisos),
            )

    async def desactivar(self, wa_id_hash: str) -> None:
        """Corta el acceso sin perder qué permisos tenía concedidos."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE ventupilot.clientes
                SET activo = false, actualizado_at = now()
                WHERE wa_id_hash = $1
                """,
                wa_id_hash,
            )
