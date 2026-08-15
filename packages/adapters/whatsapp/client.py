"""Cliente de la Graph API de WhatsApp.

**Ninguna otra parte del código hace POST a Graph API.** Concentrarlo aquí es lo
que da un solo sitio donde poner reintentos, rate limiting y el chequeo de la
ventana de 24 horas cuando llegue.

Sobre la ventana: fuera de las 24h desde el último mensaje del usuario, Meta
rechaza cualquier mensaje que no sea una plantilla aprobada. El error llega como
código 131047 y no es reintentable — reintentarlo solo gasta cuota y empeora el
quality rating del número.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger("whatsapp.client")

# Errores de Meta que no mejoran reintentando.
# 131047: fuera de la ventana de 24h, hace falta plantilla.
# 131026: el número no puede recibir mensajes.
# 100:    parámetro inválido — el payload está mal construido.
CODIGOS_NO_REINTENTABLES = frozenset({100, 131026, 131047})


class ErrorWhatsApp(Exception):
    """Fallo al enviar. `reintentable` decide si vuelve a la cola o se descarta."""

    def __init__(self, mensaje: str, *, codigo: int | None = None, reintentable: bool = True):
        super().__init__(mensaje)
        self.codigo = codigo
        self.reintentable = reintentable


@dataclass(frozen=True, slots=True)
class Enviado:
    wa_message_id: str


def cuerpo_texto(wa_id: str, texto: str) -> dict[str, Any]:
    """Construye el cuerpo de un mensaje de texto.

    Separado del envío para poder persistirlo en el outbox y para poder testear
    la forma del payload sin tocar la red.
    """
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": wa_id,
        "type": "text",
        "text": {"preview_url": False, "body": texto},
    }


class WhatsAppClient:
    """Envía mensajes por Meta directo o a través de Kapso.

    Kapso expone un proxy compatible con Graph API: mismo verbo, misma ruta a
    partir de la versión, y **el mismo cuerpo JSON**. Por eso el transporte no
    afecta a `cuerpo_texto` ni a los cuerpos interactivos: solo cambian el host
    y la cabecera de autenticación. Mantenerlo así es lo que permite cambiar de
    proveedor sin tocar el agente.
    """

    def __init__(
        self,
        access_token: str,
        phone_number_id: str,
        graph_version: str = "v23.0",
        cliente: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
        *,
        transporte: str = "meta",
        kapso_api_key: str = "",
        kapso_base_url: str = "https://api.kapso.ai",
    ) -> None:
        self._token = access_token
        self._phone_number_id = phone_number_id
        # Fijada a propósito: "latest" cambia bajo los pies sin aviso.
        self._version = graph_version
        self._timeout = timeout
        self._cliente = cliente
        self._propio = cliente is None
        self._transporte = transporte
        self._kapso_api_key = kapso_api_key
        self._kapso_base_url = kapso_base_url.rstrip("/")

    @property
    def _url(self) -> str:
        if self._transporte == "kapso":
            return (
                f"{self._kapso_base_url}/meta/whatsapp/{self._version}"
                f"/{self._phone_number_id}/messages"
            )
        return (
            f"https://graph.facebook.com/{self._version}"
            f"/{self._phone_number_id}/messages"
        )

    @property
    def _headers(self) -> dict[str, str]:
        if self._transporte == "kapso":
            return {"X-API-Key": self._kapso_api_key}
        return {"Authorization": f"Bearer {self._token}"}

    async def _http(self) -> httpx.AsyncClient:
        if self._cliente is None:
            self._cliente = httpx.AsyncClient(timeout=self._timeout)
        return self._cliente

    async def cerrar(self) -> None:
        if self._cliente is not None and self._propio:
            await self._cliente.aclose()
            self._cliente = None

    async def enviar(self, cuerpo: dict[str, Any]) -> Enviado:
        """Envía un mensaje ya construido.

        Raises:
            ErrorWhatsApp: con `reintentable` indicando si tiene sentido volver
                a intentarlo. Un 4xx por ventana cerrada no mejora con el
                tiempo; un 5xx o un timeout sí.
        """
        http = await self._http()

        try:
            r = await http.post(self._url, json=cuerpo, headers=self._headers)
        except httpx.TimeoutException as e:
            raise ErrorWhatsApp(f"timeout hacia Graph API: {e}", reintentable=True) from e
        except httpx.HTTPError as e:
            raise ErrorWhatsApp(f"error de red: {e}", reintentable=True) from e

        if r.status_code >= 500:
            raise ErrorWhatsApp(
                f"Graph API respondió {r.status_code}", reintentable=True
            )

        try:
            datos: dict[str, Any] = r.json()
        except ValueError as e:
            raise ErrorWhatsApp(
                f"respuesta no-JSON con status {r.status_code}", reintentable=True
            ) from e

        if r.status_code >= 400 or "error" in datos:
            error = datos.get("error") or {}
            codigo = error.get("code")
            reintentable = codigo not in CODIGOS_NO_REINTENTABLES
            raise ErrorWhatsApp(
                f"{error.get('message', 'error desconocido')} (código {codigo})",
                codigo=codigo,
                reintentable=reintentable,
            )

        mensajes = datos.get("messages") or []
        if not mensajes or not mensajes[0].get("id"):
            # 200 sin id: no sabemos si se entregó. Reintentar podría duplicar,
            # así que se marca no reintentable y queda registrado para revisión.
            raise ErrorWhatsApp(
                "Graph API devolvió 200 sin wa_message_id", reintentable=False
            )

        return Enviado(wa_message_id=mensajes[0]["id"])
