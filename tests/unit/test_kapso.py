"""Tests del transporte Kapso.

Kapso es un proxy compatible con Meta, así que lo que hay que verificar no es
que funcione algo nuevo, sino que **el cuerpo del mensaje no cambie** y que la
firma se compruebe con la cabecera y el secreto correctos. Una confusión ahí
produce un 403 permanente que parece un secreto mal copiado.
"""

from __future__ import annotations

import httpx
import pytest

from packages.adapters.config import Settings
from packages.adapters.whatsapp.client import ErrorWhatsApp, WhatsAppClient, cuerpo_texto
from packages.adapters.whatsapp.signature import (
    firmar,
    verificar_firma,
    verificar_firma_kapso,
)

SECRETO = "secreto-de-webhook"
CUERPO = b'{"entry":[{"id":"WABA"}]}'


def _settings(**extra: object) -> Settings:
    base: dict[str, object] = {
        "wa_app_secret": "app-secret",
        "wa_verify_token": "verify",
        "wa_id_pepper": "p" * 32,
        "database_url": "postgres://x",
    }
    base.update(extra)
    return Settings(**base)  # type: ignore[arg-type]


# ── Firma ────────────────────────────────────────────────────────────────────


def test_kapso_acepta_hex_pelado():
    """Kapso manda el hex sin el prefijo `sha256=` que usa Meta."""
    hex_pelado = firmar(CUERPO, SECRETO).removeprefix("sha256=")
    assert verificar_firma_kapso(CUERPO, hex_pelado, SECRETO)


def test_kapso_tolera_el_prefijo_por_si_lo_añaden():
    assert verificar_firma_kapso(CUERPO, firmar(CUERPO, SECRETO), SECRETO)


def test_kapso_rechaza_cuerpo_alterado():
    hex_pelado = firmar(CUERPO, SECRETO).removeprefix("sha256=")
    assert not verificar_firma_kapso(CUERPO + b" ", hex_pelado, SECRETO)


@pytest.mark.parametrize("header", [None, "", "no-es-hex"])
def test_kapso_falla_cerrado(header):
    assert not verificar_firma_kapso(CUERPO, header, SECRETO)


def test_kapso_sin_secreto_falla_cerrado():
    """Sin secreto no hay verificación posible: nunca abrir por defecto."""
    hex_pelado = firmar(CUERPO, SECRETO).removeprefix("sha256=")
    assert not verificar_firma_kapso(CUERPO, hex_pelado, "")


def test_las_dos_firmas_no_son_intercambiables():
    """El hex pelado no vale para Meta, que exige el prefijo."""
    hex_pelado = firmar(CUERPO, SECRETO).removeprefix("sha256=")
    assert not verificar_firma(CUERPO, hex_pelado, SECRETO)


# ── Selección de secreto ─────────────────────────────────────────────────────


def test_el_secreto_de_firma_depende_del_transporte():
    meta = _settings(wa_transporte="meta")
    kapso = _settings(wa_transporte="kapso", kapso_webhook_secret="wh-secret")
    assert meta.secreto_de_firma == "app-secret"
    assert kapso.secreto_de_firma == "wh-secret"


# ── Envío ────────────────────────────────────────────────────────────────────


async def _capturar(transporte: str, **kw) -> httpx.Request:
    """Envía un mensaje contra un transporte falso y devuelve la petición."""
    capturada: dict[str, httpx.Request] = {}

    def responder(request: httpx.Request) -> httpx.Response:
        capturada["r"] = request
        return httpx.Response(200, json={"messages": [{"id": "wamid.OUT"}]})

    http = httpx.AsyncClient(transport=httpx.MockTransport(responder))
    cliente = WhatsAppClient(
        access_token="token-meta",
        phone_number_id="PNID",
        graph_version="v24.0",
        cliente=http,
        transporte=transporte,
        **kw,
    )
    await cliente.enviar(cuerpo_texto("56911112222", "hola"))
    await http.aclose()
    return capturada["r"]


async def test_meta_va_a_graph_con_bearer():
    r = await _capturar("meta")
    assert r.url.host == "graph.facebook.com"
    assert str(r.url).endswith("/v24.0/PNID/messages")
    assert r.headers["authorization"] == "Bearer token-meta"


async def test_kapso_va_a_su_proxy_con_api_key():
    r = await _capturar("kapso", kapso_api_key="clave-kapso")
    assert r.url.host == "api.kapso.ai"
    assert str(r.url).endswith("/meta/whatsapp/v24.0/PNID/messages")
    assert r.headers["x-api-key"] == "clave-kapso"
    # El token de Meta no debe viajar a un tercero.
    assert "authorization" not in r.headers


async def test_el_cuerpo_es_identico_en_ambos_transportes():
    """Es la premisa de todo el cambio: si el cuerpo divergiera, no bastaría
    con cambiar la URL y habría que tocar el render de las cotizaciones."""
    meta = await _capturar("meta")
    kapso = await _capturar("kapso", kapso_api_key="clave-kapso")
    assert meta.content == kapso.content


# ── Forma del error ──────────────────────────────────────────────────────────


async def _enviar_con_respuesta(status: int, payload: dict) -> Exception | None:
    """Envía contra una respuesta fija y devuelve la excepción, si la hubo."""

    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    http = httpx.AsyncClient(transport=httpx.MockTransport(responder))
    cliente = WhatsAppClient(
        access_token="t", phone_number_id="PNID", cliente=http,
        transporte="kapso", kapso_api_key="k",
    )
    try:
        await cliente.enviar(cuerpo_texto("56911112222", "hola"))
    except Exception as exc:  # noqa: BLE001 - es justo lo que se prueba
        return exc
    finally:
        await http.aclose()
    return None


async def test_error_como_string_no_revienta():
    """Kapso devuelve {"error": "texto"}; Meta devuelve un objeto.

    Asumir la forma de Meta lanzaba AttributeError dentro del bucle de
    despacho del worker y lo tumbaba entero, en vez de fallar solo ese envío.
    """
    exc = await _enviar_con_respuesta(401, {"error": "invalid api key"})
    assert isinstance(exc, ErrorWhatsApp)
    assert "invalid api key" in str(exc)
    # Sin código no se puede afirmar que sea permanente: se reintenta.
    assert exc.reintentable


async def test_error_como_objeto_de_meta_sigue_funcionando():
    exc = await _enviar_con_respuesta(
        400, {"error": {"message": "fuera de ventana", "code": 131047}}
    )
    assert isinstance(exc, ErrorWhatsApp)
    assert exc.codigo == 131047
    # 131047 es la ventana de 24h: reintentar no la reabre.
    assert not exc.reintentable


async def test_error_sin_forma_reconocible_no_revienta():
    exc = await _enviar_con_respuesta(500, {"detail": "algo pasó"})
    assert isinstance(exc, ErrorWhatsApp)
    assert exc.reintentable
