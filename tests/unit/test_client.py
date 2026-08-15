"""Tests del cliente de Graph API.

Lo que importa aquí es la clasificación de errores: distinguir lo que mejora
reintentando de lo que no. Insistir con un error no reintentable gasta cuota y
degrada el quality rating del número, que es un recurso que no se recupera
rápido.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from packages.adapters.whatsapp.client import (
    ErrorWhatsApp,
    WhatsAppClient,
    cuerpo_texto,
)

TOKEN = "token-de-prueba"
PNID = "1234567890"


def _cliente(handler: Any) -> WhatsAppClient:
    transporte = httpx.MockTransport(handler)
    return WhatsAppClient(
        access_token=TOKEN,
        phone_number_id=PNID,
        cliente=httpx.AsyncClient(transport=transporte),
    )


def test_cuerpo_texto_desactiva_la_previsualizacion() -> None:
    """Sin esto, un SKU que parezca URL genera una tarjeta de preview absurda."""
    cuerpo = cuerpo_texto("56912345678", "hola")
    assert cuerpo["text"]["preview_url"] is False
    assert cuerpo["messaging_product"] == "whatsapp"
    assert cuerpo["to"] == "56912345678"


async def test_envio_exitoso_devuelve_el_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert PNID in str(request.url)
        return httpx.Response(200, json={"messages": [{"id": "wamid.NUEVO"}]})

    cliente = _cliente(handler)
    enviado = await cliente.enviar(cuerpo_texto("56912345678", "hola"))
    assert enviado.wa_message_id == "wamid.NUEVO"


async def test_la_version_de_graph_va_en_la_url() -> None:
    """Fijar la versión evita que un cambio de Meta rompa el envío sin aviso."""
    vistas: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        vistas.append(str(request.url))
        return httpx.Response(200, json={"messages": [{"id": "x"}]})

    cliente = WhatsAppClient(
        access_token=TOKEN,
        phone_number_id=PNID,
        graph_version="v21.0",
        cliente=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    await cliente.enviar(cuerpo_texto("56912345678", "hola"))
    assert "/v21.0/" in vistas[0]


async def test_ventana_de_24h_cerrada_no_es_reintentable() -> None:
    """131047: hace falta plantilla. Reintentar nunca lo va a arreglar."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "Re-engagement message",
                    "code": 131047,
                }
            },
        )

    cliente = _cliente(handler)
    with pytest.raises(ErrorWhatsApp) as exc:
        await cliente.enviar(cuerpo_texto("56912345678", "hola"))

    assert exc.value.codigo == 131047
    assert exc.value.reintentable is False


async def test_error_5xx_si_es_reintentable() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream caído")

    cliente = _cliente(handler)
    with pytest.raises(ErrorWhatsApp) as exc:
        await cliente.enviar(cuerpo_texto("56912345678", "hola"))
    assert exc.value.reintentable is True


async def test_error_transitorio_de_meta_si_es_reintentable() -> None:
    """Un código de error desconocido se asume transitorio, no permanente."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "límite", "code": 130429}})

    cliente = _cliente(handler)
    with pytest.raises(ErrorWhatsApp) as exc:
        await cliente.enviar(cuerpo_texto("56912345678", "hola"))
    assert exc.value.reintentable is True


async def test_timeout_es_reintentable() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("agotado")

    cliente = _cliente(handler)
    with pytest.raises(ErrorWhatsApp) as exc:
        await cliente.enviar(cuerpo_texto("56912345678", "hola"))
    assert exc.value.reintentable is True


async def test_200_sin_id_no_se_reintenta() -> None:
    """No sabemos si se entregó; reintentar podría duplicarlo para el cliente."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"messages": []})

    cliente = _cliente(handler)
    with pytest.raises(ErrorWhatsApp) as exc:
        await cliente.enviar(cuerpo_texto("56912345678", "hola"))
    assert exc.value.reintentable is False
