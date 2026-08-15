"""Tests del render de la cotización.

Este texto es lo que el cliente lee. Un separador de miles mal puesto en una
cifra chilena cambia el número que la persona entiende.
"""

from __future__ import annotations

from datetime import UTC, datetime

from packages.agents.mensajes import (
    PREFIJO_CONFIRMAR,
    PREFIJO_RECHAZAR,
    cuerpo_propuesta,
    formatear_clp,
    texto_propuesta,
)
from packages.domain.propuesta import SeleccionLinea, construir_propuesta

AHORA = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


def _propuesta():
    return construir_propuesta(
        wa_id_hash="h",
        wa_message_id="wamid.1",
        seleccion=[SeleccionLinea(sku="SKU-1", cantidad=2)],
        precios={"SKU-1": ("Notebook 14", 499_990)},
        ttl_min=30,
        ahora=AHORA,
    )


def test_formato_chileno_usa_punto_de_miles():
    assert formatear_clp(1_234_567) == "$1.234.567"
    assert formatear_clp(990) == "$990"
    assert formatear_clp(0) == "$0"


def test_texto_incluye_lineas_y_total():
    cuerpo = texto_propuesta(_propuesta())
    assert "2 × Notebook 14" in cuerpo
    assert "$499.990" in cuerpo
    assert "$999.980" in cuerpo


def test_los_botones_llevan_el_id_de_la_propuesta():
    """Invariante 2: la confirmación es estructurada, no texto libre."""
    p = _propuesta()
    cuerpo = cuerpo_propuesta("56912345678", p)

    botones = cuerpo["interactive"]["action"]["buttons"]
    ids = [b["reply"]["id"] for b in botones]
    assert f"{PREFIJO_CONFIRMAR}{p.propuesta_id}" in ids
    assert f"{PREFIJO_RECHAZAR}{p.propuesta_id}" in ids


def test_titulos_de_boton_dentro_del_limite_de_meta():
    """Meta rechaza el mensaje entero si un título pasa de 20 caracteres."""
    botones = cuerpo_propuesta("56912345678", _propuesta())["interactive"]["action"]["buttons"]
    for b in botones:
        assert len(b["reply"]["title"]) <= 20


def test_los_prefijos_no_colisionan():
    """El handler distingue confirmar de rechazar por prefijo."""
    assert not PREFIJO_CONFIRMAR.startswith(PREFIJO_RECHAZAR)
    assert not PREFIJO_RECHAZAR.startswith(PREFIJO_CONFIRMAR)
