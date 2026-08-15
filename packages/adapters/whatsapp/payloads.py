"""Parseo del payload anidado del webhook de WhatsApp Cloud API.

El payload de Meta viene en tres niveles de anidación
(`entry[] → changes[] → value`) y un mismo POST puede traer varios mensajes de
varios remitentes. Este módulo lo aplana a una lista de `MensajeEntrante`.

Dos criterios de diseño:

1. **Tolerancia a lo desconocido.** Meta añade tipos de mensaje sin avisar. Un
   tipo que no reconocemos se clasifica como no soportado y sigue el flujo
   normal —el usuario recibe una respuesta— en vez de reventar el parser y
   provocar que Meta reintente el webhook indefinidamente.

2. **Los no soportados son explícitos.** Audio, imágenes y documentos son
   comunes en venta B2B por WhatsApp. Que el sistema los rechace es una
   decisión de producto, no un KeyError.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class TipoMensaje(StrEnum):
    """Tipos que el agente sabe procesar, más el cajón de los que no."""

    TEXTO = "texto"
    # Respuesta a botón o lista interactiva. Es el canal de confirmación de
    # propuestas: estructurado, no texto libre (invariante 2).
    INTERACTIVO = "interactivo"
    # Respuesta a un botón de plantilla (quick reply).
    BOTON_PLANTILLA = "boton_plantilla"
    # Reconocido pero deliberadamente no procesado en v1.
    NO_SOPORTADO = "no_soportado"


class MensajeEntrante(BaseModel):
    """Un mensaje de usuario, normalizado y listo para encolar."""

    wa_message_id: str
    wa_id: str
    phone_number_id: str
    tipo: TipoMensaje
    # Texto del mensaje, o el título del botón/opción elegida.
    texto: str | None = None
    # ID estructurado de la opción elegida (botón o item de lista). Es lo que
    # correlaciona una confirmación con su propuesta; nunca se infiere del texto.
    payload_id: str | None = None
    # Subtipo crudo de Meta cuando tipo == NO_SOPORTADO: "audio", "image", etc.
    subtipo_original: str | None = None
    # wa_message_id del mensaje citado, si el usuario respondió a uno anterior.
    contexto_id: str | None = None
    nombre_perfil: str | None = None
    recibido_at: datetime
    enviado_at: datetime | None = None


class EstadoMensaje(BaseModel):
    """Acuse de entrega de un mensaje que enviamos nosotros.

    No dispara al agente, pero alimenta las métricas de entrega y el quality
    rating. Se parsea aparte para no confundirlo con un mensaje de usuario.
    """

    wa_message_id: str
    estado: str  # sent | delivered | read | failed
    wa_id: str | None = None
    ocurrido_at: datetime | None = None
    error: dict[str, Any] | None = None


class LotePayload(BaseModel):
    """Resultado de aplanar un POST del webhook."""

    mensajes: list[MensajeEntrante] = Field(default_factory=list)
    estados: list[EstadoMensaje] = Field(default_factory=list)


_TIPOS_MEDIA = frozenset(
    {"image", "audio", "video", "document", "sticker", "voice"}
)


def _ts(valor: Any) -> datetime | None:
    """Convierte el timestamp de Meta (segundos epoch como string) a datetime."""
    if valor is None:
        return None
    try:
        return datetime.fromtimestamp(int(valor), tz=UTC)
    except (TypeError, ValueError):
        return None


def _parsear_interactivo(interactive: dict[str, Any]) -> tuple[str | None, str | None]:
    """Extrae (payload_id, texto) de un mensaje interactivo.

    Cubre respuestas de botón, de lista y de WhatsApp Flows (`nfm_reply`).
    """
    subtipo = interactive.get("type")

    if subtipo == "button_reply":
        r = interactive.get("button_reply") or {}
        return r.get("id"), r.get("title")

    if subtipo == "list_reply":
        r = interactive.get("list_reply") or {}
        return r.get("id"), r.get("title")

    if subtipo == "nfm_reply":
        r = interactive.get("nfm_reply") or {}
        # El payload de un Flow llega como JSON serializado en response_json.
        return r.get("name"), r.get("response_json")

    return None, None


def _parsear_mensaje(
    msg: dict[str, Any],
    phone_number_id: str,
    nombres: dict[str, str],
    recibido_at: datetime,
) -> MensajeEntrante | None:
    wa_message_id = msg.get("id")
    wa_id = msg.get("from")
    if not wa_message_id or not wa_id:
        # Sin identificador no hay dedupe posible ni destinatario de respuesta.
        return None

    tipo_meta = msg.get("type")
    texto: str | None = None
    payload_id: str | None = None
    subtipo_original: str | None = None

    if tipo_meta == "text":
        tipo = TipoMensaje.TEXTO
        texto = (msg.get("text") or {}).get("body")
    elif tipo_meta == "interactive":
        tipo = TipoMensaje.INTERACTIVO
        payload_id, texto = _parsear_interactivo(msg.get("interactive") or {})
    elif tipo_meta == "button":
        tipo = TipoMensaje.BOTON_PLANTILLA
        boton = msg.get("button") or {}
        payload_id = boton.get("payload")
        texto = boton.get("text")
    else:
        # Media, ubicación, contactos, reacciones, pedidos del catálogo nativo
        # y cualquier tipo que Meta añada en el futuro.
        tipo = TipoMensaje.NO_SOPORTADO
        subtipo_original = tipo_meta if isinstance(tipo_meta, str) else None
        if tipo_meta in _TIPOS_MEDIA:
            # El caption suele traer la intención real ("cotiza esto x100").
            texto = (msg.get(tipo_meta) or {}).get("caption")

    return MensajeEntrante(
        wa_message_id=wa_message_id,
        wa_id=wa_id,
        phone_number_id=phone_number_id,
        tipo=tipo,
        texto=texto,
        payload_id=payload_id,
        subtipo_original=subtipo_original,
        contexto_id=(msg.get("context") or {}).get("id"),
        nombre_perfil=nombres.get(wa_id),
        recibido_at=recibido_at,
        enviado_at=_ts(msg.get("timestamp")),
    )


def parsear_webhook(payload: dict[str, Any], recibido_at: datetime | None = None) -> LotePayload:
    """Aplana un POST del webhook a mensajes y acuses.

    Nunca lanza por un payload inesperado: lo que no se reconoce se ignora o se
    marca como no soportado. Un webhook que devuelve 500 hace que Meta reintente
    y, si persiste, degrada el quality rating del número.
    """
    recibido = recibido_at or datetime.now(tz=UTC)
    lote = LotePayload()

    if not isinstance(payload, dict):
        return lote

    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue

        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue

            value = change.get("value")
            if not isinstance(value, dict):
                continue

            phone_number_id = (value.get("metadata") or {}).get("phone_number_id") or ""

            nombres: dict[str, str] = {}
            for contacto in value.get("contacts") or []:
                if not isinstance(contacto, dict):
                    continue
                wa_id = contacto.get("wa_id")
                nombre = (contacto.get("profile") or {}).get("name")
                if wa_id and nombre:
                    nombres[wa_id] = nombre

            for msg in value.get("messages") or []:
                if not isinstance(msg, dict):
                    continue
                parsed = _parsear_mensaje(msg, phone_number_id, nombres, recibido)
                if parsed is not None:
                    lote.mensajes.append(parsed)

            for st in value.get("statuses") or []:
                if not isinstance(st, dict) or not st.get("id"):
                    continue
                lote.estados.append(
                    EstadoMensaje(
                        wa_message_id=st["id"],
                        estado=st.get("status") or "desconocido",
                        wa_id=st.get("recipient_id"),
                        ocurrido_at=_ts(st.get("timestamp")),
                        error=(st.get("errors") or [None])[0],
                    )
                )

    return lote
