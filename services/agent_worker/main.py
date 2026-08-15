"""agent-worker — consume la cola, procesa y responde.

Dos bucles concurrentes en el mismo proceso:

- **procesar**: toma una tarea con el advisory lock de su conversación, produce
  una respuesta y la deja en el outbox. Todo en una transacción.
- **despachar**: lee el outbox y envía a Graph API.

Están separados a propósito. Si el envío viviera dentro de la transacción de
procesamiento, un timeout de Graph API dejaría la transacción abierta reteniendo
el lock de la conversación, y el usuario no podría ser atendido hasta que
expirara.

En Fase 1 el handler es un eco. En Fase 2 se sustituye por el agente sin tocar
nada de este archivo salvo la línea que lo elige.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Any, Protocol

import asyncpg

from packages.adapters import outbox
from packages.adapters.cola import ColaRepo, Tarea
from packages.adapters.config import Settings, get_settings
from packages.adapters.whatsapp.client import ErrorWhatsApp, WhatsAppClient, cuerpo_texto
from packages.adapters.whatsapp.identity import enmascarar, hash_wa_id
from packages.adapters.whatsapp.payloads import MensajeEntrante, TipoMensaje

log = logging.getLogger("agent_worker")

INTERVALO_VACIO = 1.0  # segundos sin trabajo antes de volver a mirar

# Política explícita para lo que v1 no procesa. Sin esto, una nota de voz —el
# canal por defecto de mucha gente en venta B2B— produce silencio, y el cliente
# no sabe si el sistema está caído o lo está ignorando.
RESPUESTA_NO_SOPORTADO = (
    "Por ahora solo puedo leer mensajes de texto. "
    "¿Me escribes qué producto necesitas y en qué cantidad?"
)


class Handler(Protocol):
    """Produce la respuesta a un mensaje. En Fase 2 lo implementa el agente."""

    async def __call__(self, mensaje: MensajeEntrante) -> str | None: ...


async def handler_eco(mensaje: MensajeEntrante) -> str | None:
    """Fase 1: prueba el transporte completo sin nada de IA."""
    if mensaje.tipo is TipoMensaje.NO_SOPORTADO:
        return RESPUESTA_NO_SOPORTADO
    if not mensaje.texto:
        return None
    return f"recibí: {mensaje.texto}"


class Worker:
    def __init__(
        self,
        pool: asyncpg.Pool,
        cliente: WhatsAppClient,
        settings: Settings,
        handler: Handler,
    ) -> None:
        self._pool = pool
        self._cliente = cliente
        self._settings = settings
        self._handler = handler
        self._cola = ColaRepo(pool)
        self._parar = asyncio.Event()

    def detener(self) -> None:
        self._parar.set()

    # ── Procesamiento ────────────────────────────────────────────────────────

    async def _procesar_una(self) -> bool:
        """Procesa una tarea. Devuelve False si no había nada que hacer."""
        async with self._pool.acquire() as conn, conn.transaction():
            tarea = await self._cola.tomar(conn)
            if tarea is None:
                return False

            try:
                await self._ejecutar(conn, tarea)
                await self._cola.completar(conn, tarea.id)
            except Exception as e:
                # No re-lanzamos: eso abortaría la transacción y perderíamos el
                # registro del intento fallido junto con el trabajo.
                log.exception("tarea %s falló", tarea.id)
                await self._cola.fallar(conn, tarea.id, str(e))
            return True

    async def _ejecutar(self, conn: asyncpg.Connection, tarea: Tarea) -> None:
        mensaje = MensajeEntrante.model_validate(tarea.payload)

        respuesta = await self._handler(mensaje)
        if respuesta is None:
            log.info(
                "sin respuesta para %s de %s",
                mensaje.wa_message_id,
                enmascarar(mensaje.wa_id),
            )
            return

        # Derivada del mensaje entrante: reprocesar la misma tarea tras un fallo
        # ambiguo no le manda al cliente la respuesta dos veces.
        clave = f"resp:{mensaje.wa_message_id}"

        await outbox.encolar_salida(
            conn,
            wa_id_hash=hash_wa_id(mensaje.wa_id, self._settings.wa_id_pepper),
            idempotency_key=clave,
            cuerpo=cuerpo_texto(mensaje.wa_id, respuesta),
        )

    # ── Despacho ─────────────────────────────────────────────────────────────

    async def _despachar_uno(self) -> bool:
        async with self._pool.acquire() as conn, conn.transaction():
            saliente = await outbox.tomar_salida(conn)
            if saliente is None:
                return False

            try:
                enviado = await self._cliente.enviar(saliente.cuerpo)
            except ErrorWhatsApp as e:
                log.warning(
                    "envío %s falló (reintentable=%s): %s",
                    saliente.id,
                    e.reintentable,
                    e,
                )
                await outbox.marcar_fallo(
                    conn, saliente.id, str(e), reintentable=e.reintentable
                )
            else:
                await outbox.marcar_enviado(conn, saliente.id, enviado.wa_message_id)
            return True

    # ── Bucles ───────────────────────────────────────────────────────────────

    async def _bucle(self, paso: Any, nombre: str) -> None:
        while not self._parar.is_set():
            try:
                hubo_trabajo = await paso()
            except Exception:
                # Un fallo de conexión no debe matar el bucle: se reintenta.
                log.exception("error en el bucle %s", nombre)
                hubo_trabajo = False

            if not hubo_trabajo:
                try:
                    await asyncio.wait_for(self._parar.wait(), timeout=INTERVALO_VACIO)
                except TimeoutError:
                    pass

    async def correr(self) -> None:
        await asyncio.gather(
            self._bucle(self._procesar_una, "procesar"),
            self._bucle(self._despachar_uno, "despachar"),
        )


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    pool = await asyncpg.create_pool(
        settings.database_url,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
    )
    cliente = WhatsAppClient(
        access_token=settings.wa_access_token,
        phone_number_id=settings.wa_phone_number_id,
        graph_version=settings.wa_graph_version,
    )
    worker = Worker(pool, cliente, settings, handler_eco)

    bucle = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        bucle.add_signal_handler(sig, worker.detener)

    log.info("worker arriba")
    try:
        await worker.correr()
    finally:
        log.info("worker cerrando")
        await cliente.cerrar()
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
