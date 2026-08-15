"""Tests de la verificación de firma del webhook.

Es el módulo más pequeño del sistema y el que más gente implementa mal, así que
los casos negativos importan más que los positivos.
"""

from __future__ import annotations

import json

import pytest

from packages.adapters.whatsapp.signature import (
    firmar,
    verificar_firma,
    verificar_handshake,
)

SECRET = "app-secret-de-prueba"
BODY = b'{"object":"whatsapp_business_account","entry":[]}'


def test_firma_valida_se_acepta() -> None:
    assert verificar_firma(BODY, firmar(BODY, SECRET), SECRET) is True


def test_firma_de_otro_secret_se_rechaza() -> None:
    ajena = firmar(BODY, "otro-secret")
    assert verificar_firma(BODY, ajena, SECRET) is False


def test_un_byte_distinto_invalida_la_firma() -> None:
    header = firmar(BODY, SECRET)
    alterado = BODY.replace(b"entry", b"entrY")
    assert verificar_firma(alterado, header, SECRET) is False


def test_reserializar_el_json_rompe_la_firma() -> None:
    """El error clásico: firmar sobre el cuerpo parseado y vuelto a serializar.

    Este test existe para que quede documentado en el propio código: si alguien
    cambia el gateway para pasar `json.dumps(await request.json())` en vez del
    body crudo, este test es el que se pone rojo.
    """
    # Meta serializa compacto, sin espacios tras `:` ni `,`. json.dumps mete
    # espacios por defecto, así que el round-trip cambia los bytes.
    crudo = b'{"object":"whatsapp_business_account","entry":[]}'
    header = firmar(crudo, SECRET)

    reserializado = json.dumps(json.loads(crudo)).encode("utf-8")

    assert reserializado != crudo
    assert verificar_firma(crudo, header, SECRET) is True
    assert verificar_firma(reserializado, header, SECRET) is False


def test_no_ascii_sobrevive_solo_en_crudo() -> None:
    """Nombres de producto con tilde o ñ son lo normal en Chile."""
    crudo = '{"texto":"Ampolleta LED cálida 9W · señalética"}'.encode()
    header = firmar(crudo, SECRET)
    assert verificar_firma(crudo, header, SECRET) is True

    # json.dumps escapa los no-ASCII por defecto: otros bytes, otra firma.
    escapado = json.dumps(json.loads(crudo)).encode("utf-8")
    assert verificar_firma(escapado, header, SECRET) is False


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "sha256=",
        "sha1=abc123",
        firmar(BODY, SECRET).removeprefix("sha256="),  # sin prefijo
        "sha256=no-es-hex",
    ],
)
def test_cabeceras_malformadas_se_rechazan(header: str | None) -> None:
    assert verificar_firma(BODY, header, SECRET) is False


def test_secret_vacio_falla_cerrado() -> None:
    """Sin secret configurado no se acepta nada, ni siquiera una firma coherente."""
    assert verificar_firma(BODY, firmar(BODY, ""), "") is False


def test_hex_en_mayusculas_se_acepta() -> None:
    header = firmar(BODY, SECRET).upper().replace("SHA256=", "sha256=")
    assert verificar_firma(BODY, header, SECRET) is True


class TestHandshake:
    def test_token_correcto_devuelve_challenge(self) -> None:
        assert verificar_handshake("subscribe", "tok", "1234", "tok") == "1234"

    def test_token_incorrecto_devuelve_none(self) -> None:
        assert verificar_handshake("subscribe", "malo", "1234", "tok") is None

    def test_modo_incorrecto_devuelve_none(self) -> None:
        assert verificar_handshake("unsubscribe", "tok", "1234", "tok") is None

    def test_sin_token_configurado_devuelve_none(self) -> None:
        assert verificar_handshake("subscribe", "", "1234", "") is None
