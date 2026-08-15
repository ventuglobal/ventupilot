"""El handler de Fase 2: sustituye al eco del worker.

`services/agent_worker/main.py` define el `Handler` como "dado un mensaje,
produce los cuerpos a enviar". Todo lo que sigue implementa eso; el worker no
cambia salvo la línea que elige handler.

Lo que este módulo decide, y el modelo no:

- **Los permisos**, antes de invocar al agente. Un remitente sin permiso no
  gasta ni un token.
- **La confirmación de una propuesta**, que se resuelve con un UPDATE
  condicional sin volver a llamar al modelo (invariante 2).
- **El total**, vía `construir_propuesta`, con precios releídos en ese
  instante y no los que el modelo vio durante la búsqueda (invariante 1).

Todo corre dentro de la transacción del worker, que tiene tomado el advisory
lock de la conversación: dos mensajes seguidos del mismo remitente no se
procesan en paralelo ni se pisan el historial.
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ModelMessagesTypeAdapter

from packages.adapters import conversaciones
from packages.adapters.catalogo import CatalogoRepo
from packages.adapters.clientes import ClientesRepo
from packages.adapters.config import Settings
from packages.adapters.propuestas import PropuestasRepo
from packages.adapters.whatsapp.client import cuerpo_texto
from packages.adapters.whatsapp.identity import enmascarar
from packages.adapters.whatsapp.payloads import MensajeEntrante, TipoMensaje
from packages.agents.agente import agente, limites
from packages.agents.deps import DepsAgente
from packages.agents.mensajes import PREFIJO_CONFIRMAR, PREFIJO_RECHAZAR, cuerpo_propuesta
from packages.domain.identidad import Permiso
from packages.domain.propuesta import EstadoPropuesta, construir_propuesta

log = logging.getLogger("agente.handler")

# Cuántos mensajes del historial se le devuelven al modelo. El tope real de
# un turno lo pone max_requests_per_run; esto solo evita que una conversación
# de meses entre entera en el prompt.
_HISTORIAL_MAX = 20

MSG_NO_AUTORIZADO = (
    "Hola. Para cotizar por este canal necesitas estar habilitado. "
    "Un ejecutivo de Ventu se pondrá en contacto contigo."
)
MSG_NO_SOPORTADO = (
    "Por ahora solo puedo leer mensajes de texto. "
    "¿Me escribes qué producto necesitas y en qué cantidad?"
)
MSG_ERROR = (
    "Tuve un problema procesando tu mensaje. Ya quedó registrado y lo "
    "revisamos. ¿Puedes intentarlo de nuevo en un momento?"
)
MSG_NO_DISPONIBLE = (
    "Esa cotización ya no está disponible: puede haber vencido o haber sido "
    "respondida antes. ¿Quieres que la prepare de nuevo?"
)
MSG_CONFIRMADA = (
    "Listo, cotización confirmada. Un ejecutivo de Ventu la toma desde aquí "
    "y te contacta para coordinar."
)
MSG_RECHAZADA = "Sin problema, la descarto. ¿Buscamos otra cosa?"


class HandlerAgente:
    """Handler de Fase 2. Devuelve los cuerpos a encolar en el outbox."""

    def __init__(
        self,
        settings: Settings,
        catalogo: CatalogoRepo,
        clientes: ClientesRepo,
        propuestas: PropuestasRepo,
    ) -> None:
        self._settings = settings
        self._catalogo = catalogo
        self._clientes = clientes
        self._propuestas = propuestas

    async def __call__(
        self, conn: asyncpg.Connection, mensaje: MensajeEntrante, wa_id_hash: str
    ) -> list[dict[str, Any]]:
        cliente = await self._clientes.obtener(wa_id_hash)

        # Sin permiso ni para conversar: se responde y se cierra, sin modelo.
        if not cliente.puede(Permiso.CONSULTAR):
            log.info("remitente no autorizado: %s", enmascarar(mensaje.wa_id))
            return [cuerpo_texto(mensaje.wa_id, MSG_NO_AUTORIZADO)]

        conversacion_id = await conversaciones.obtener_o_crear(
            conn, wa_id_hash, mensaje.phone_number_id
        )
        await conversaciones.marcar_mensaje_usuario(conn, conversacion_id)

        # Botón de una propuesta: transición de estado con impacto económico.
        # El camino más corto entre el botón y el UPDATE es el más auditable.
        if mensaje.tipo in (TipoMensaje.INTERACTIVO, TipoMensaje.BOTON_PLANTILLA):
            respuesta = await self._resolver_boton(conn, mensaje, wa_id_hash)
            if respuesta is not None:
                return [cuerpo_texto(mensaje.wa_id, respuesta)]

        texto = (mensaje.texto or "").strip()
        if not texto:
            return [cuerpo_texto(mensaje.wa_id, MSG_NO_SOPORTADO)]

        return await self._correr_agente(conn, mensaje, wa_id_hash, cliente, conversacion_id, texto)

    async def _resolver_boton(
        self, conn: asyncpg.Connection, mensaje: MensajeEntrante, wa_id_hash: str
    ) -> str | None:
        """Resuelve una pulsación. Devuelve None si el botón no es nuestro."""
        payload_id = mensaje.payload_id or ""
        if payload_id.startswith(PREFIJO_CONFIRMAR):
            estado = EstadoPropuesta.CONFIRMADA
            propuesta_id = payload_id[len(PREFIJO_CONFIRMAR) :]
            exito = MSG_CONFIRMADA
        elif payload_id.startswith(PREFIJO_RECHAZAR):
            estado = EstadoPropuesta.RECHAZADA
            propuesta_id = payload_id[len(PREFIJO_RECHAZAR) :]
            exito = MSG_RECHAZADA
        else:
            return None

        cambio = await self._propuestas.resolver(
            conn, propuesta_id, estado, wa_id_hash=wa_id_hash
        )
        return exito if cambio else MSG_NO_DISPONIBLE

    async def _correr_agente(
        self,
        conn: asyncpg.Connection,
        mensaje: MensajeEntrante,
        wa_id_hash: str,
        cliente: Any,
        conversacion_id: int,
        texto: str,
    ) -> list[dict[str, Any]]:
        crudo = await conversaciones.cargar_historial(
            conn, conversacion_id, limite=_HISTORIAL_MAX
        )
        historial = ModelMessagesTypeAdapter.validate_python(crudo) if crudo else None

        deps = DepsAgente(
            catalogo=self._catalogo,
            settings=self._settings,
            wa_id_hash=wa_id_hash,
            cliente=cliente,
            wa_message_id=mensaje.wa_message_id,
        )

        try:
            resultado = await agente.run(
                texto,
                deps=deps,
                message_history=historial,
                usage_limits=limites(self._settings),
            )
        except UsageLimitExceeded:
            # El run se pasó de vueltas: fallo de diseño nuestro o consulta
            # patológica. En ningún caso culpa del cliente.
            log.warning("límite de run excedido en %s", mensaje.wa_message_id)
            return [cuerpo_texto(mensaje.wa_id, MSG_ERROR)]

        # Se serializa con el TypeAdapter de pydantic-ai, no campo a campo: es
        # el mismo que los vuelve a leer en `cargar_historial`, así que las
        # tool calls y sus resultados sobreviven al viaje de ida y vuelta.
        await conversaciones.anexar_historial(
            conn,
            conversacion_id,
            ModelMessagesTypeAdapter.dump_python(resultado.new_messages(), mode="json"),
        )

        salida = resultado.output
        cuerpos = [cuerpo_texto(mensaje.wa_id, salida.mensaje)] if salida.mensaje else []

        if not salida.proponer or not salida.lineas:
            return cuerpos

        # Cinturón además de tirantes: el modelo tiene instrucciones de no
        # proponer a quien no puede cotizar, pero una instrucción no es un
        # control. Este `if` sí lo es.
        if not cliente.puede(Permiso.COTIZAR):
            log.warning("el modelo propuso a un cliente sin permiso de cotizar")
            return cuerpos

        precios = await self._catalogo.precios_por_sku(
            [ln.sku for ln in salida.lineas],
            canal=self._settings.pricing_channel,
            max_edad_horas=self._settings.precio_max_edad_horas,
        )

        try:
            propuesta = construir_propuesta(
                wa_id_hash=wa_id_hash,
                wa_message_id=mensaje.wa_message_id,
                seleccion=salida.lineas,
                precios=precios,
                ttl_min=self._settings.propuesta_ttl_min,
            )
        except ValueError as exc:
            # El modelo eligió un SKU sin precio vigente. Se manda su texto
            # sin cotización antes que inventar un total.
            log.warning("no se pudo valorizar la propuesta: %s", exc)
            return cuerpos

        # Si ya existía, este mensaje ya se procesó: no se reenvía nada.
        if not await self._propuestas.guardar(conn, propuesta, conversacion_id=conversacion_id):
            log.info("propuesta %s ya existía", propuesta.propuesta_id)
            return []

        cuerpos.append(cuerpo_propuesta(mensaje.wa_id, propuesta))
        return cuerpos
