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
from packages.adapters.catalogo import CatalogoRepo
from packages.adapters.clientes import ClientesRepo
from packages.adapters.cola import ColaRepo, Tarea
from packages.adapters.config import Settings, get_settings
from packages.adapters.propuestas import PropuestasRepo
from packages.adapters.whatsapp.client import ErrorWhatsApp, WhatsAppClient, cuerpo_texto
from packages.adapters.whatsapp.identity import enmascarar, hash_wa_id
from packages.adapters.whatsapp.payloads import MensajeEntrante, TipoMensaje
from packages.agents.handler import HandlerAgente

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
    """Produce los mensajes de respuesta. En Fase 2 lo implementa el agente.

    Devuelve cuerpos de Graph API ya formados —no texto— porque una respuesta
    puede necesitar botones: la cotización se confirma con un elemento
    estructurado, no escribiendo "sí" (invariante 2). Una lista vacía
    significa "no hay nada que responder".

    Recibe la conexión de la transacción del worker para que lo que escriba
    —conversación, historial, propuesta— entre o no entre junto con el
    encolado en el outbox, sin estados a medias.
    """

    async def __call__(
        self, conn: asyncpg.Connection, mensaje: MensajeEntrante, wa_id_hash: str
    ) -> list[dict[str, Any]]: ...


async def handler_eco(
    conn: asyncpg.Connection, mensaje: MensajeEntrante, wa_id_hash: str
) -> list[dict[str, Any]]:
    """Fase 1: prueba el transporte completo sin nada de IA.

    Se conserva para poder diagnosticar el transporte sin el agente de por
    medio: si el eco llega y el agente no, el problema no está en Meta.
    """
    if mensaje.tipo is TipoMensaje.NO_SOPORTADO:
        return [cuerpo_texto(mensaje.wa_id, RESPUESTA_NO_SOPORTADO)]
    if not mensaje.texto:
        return []
    return [cuerpo_texto(mensaje.wa_id, f"recibí: {mensaje.texto}")]


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
        wa_id_hash = hash_wa_id(mensaje.wa_id, self._settings.wa_id_pepper)

        cuerpos = await self._handler(conn, mensaje, wa_id_hash)
        if not cuerpos:
            log.info(
                "sin respuesta para %s de %s",
                mensaje.wa_message_id,
                enmascarar(mensaje.wa_id),
            )
            return

        for i, cuerpo in enumerate(cuerpos):
            # Derivada del mensaje entrante y de la posición: reprocesar la
            # misma tarea tras un fallo ambiguo no le manda al cliente las
            # respuestas dos veces, y el índice las distingue entre sí cuando
            # un turno produce texto y cotización.
            await outbox.encolar_salida(
                conn,
                wa_id_hash=wa_id_hash,
                idempotency_key=f"resp:{mensaje.wa_message_id}:{i}",
                cuerpo=cuerpo,
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
    # Pool aparte con el rol de solo lectura, exclusivo para el catálogo. Es
    # lo que hace que un prompt injection no tenga privilegio que escalar: la
    # tool que el modelo puede invocar no alcanza el pool de escritura.
    # Sin DATABASE_URL_RO se cae al principal, que sirve para desarrollo pero
    # pierde esa garantía; por eso se avisa.
    if settings.database_url_ro:
        pool_ro = await asyncpg.create_pool(
            settings.database_url_ro,
            min_size=settings.db_pool_min,
            max_size=settings.db_pool_max,
        )
    else:
        log.warning(
            "DATABASE_URL_RO sin definir: el catálogo se lee con el rol de "
            "escritura. No lo dejes así en producción."
        )
        pool_ro = pool

    cliente = WhatsAppClient(
        access_token=settings.wa_access_token,
        phone_number_id=settings.wa_phone_number_id,
        graph_version=settings.wa_graph_version,
        transporte=settings.wa_transporte,
        kapso_api_key=settings.kapso_api_key,
        kapso_base_url=settings.kapso_base_url,
    )

    handler = HandlerAgente(
        settings=settings,
        catalogo=CatalogoRepo(pool_ro),
        clientes=ClientesRepo(pool),
        propuestas=PropuestasRepo(pool),
    )
    worker = Worker(pool, cliente, settings, handler)

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
