"""Tests de valorización de propuestas.

Es la única aritmética sobre dinero del sistema, así que se prueba de forma
desproporcionada respecto a su tamaño.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from packages.domain.propuesta import (
    EstadoPropuesta,
    SeleccionLinea,
    construir_propuesta,
    id_propuesta,
)

AHORA = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
PRECIOS = {
    "SKU-1": ("Notebook 14 pulgadas", 499_990),
    "SKU-2": ("Mouse inalámbrico", 12_990),
}


def test_valoriza_y_totaliza():
    p = construir_propuesta(
        wa_id_hash="h",
        wa_message_id="wamid.1",
        seleccion=[
            SeleccionLinea(sku="SKU-1", cantidad=2),
            SeleccionLinea(sku="SKU-2", cantidad=3),
        ],
        precios=PRECIOS,
        ttl_min=30,
        ahora=AHORA,
    )

    assert [ln.subtotal_clp for ln in p.lineas] == [999_980, 38_970]
    assert p.total_clp == 1_038_950
    assert p.estado is EstadoPropuesta.VIGENTE
    assert p.expira_at == AHORA + timedelta(minutes=30)


def test_toma_titulo_del_backend_no_del_modelo():
    """El título que se muestra sale del catálogo, no de lo que diga el modelo."""
    p = construir_propuesta(
        wa_id_hash="h",
        wa_message_id="wamid.1",
        seleccion=[SeleccionLinea(sku="SKU-1", cantidad=1)],
        precios=PRECIOS,
        ttl_min=30,
        ahora=AHORA,
    )
    assert p.lineas[0].titulo == "Notebook 14 pulgadas"


def test_sku_sin_precio_vigente_falla():
    """Antes cotizar un SKU inventado a cero que responder con un total falso."""
    with pytest.raises(ValueError, match="SKU-9"):
        construir_propuesta(
            wa_id_hash="h",
            wa_message_id="wamid.1",
            seleccion=[SeleccionLinea(sku="SKU-9", cantidad=1)],
            precios=PRECIOS,
            ttl_min=30,
            ahora=AHORA,
        )


def test_seleccion_vacia_falla():
    with pytest.raises(ValueError, match="vacía"):
        construir_propuesta(
            wa_id_hash="h",
            wa_message_id="wamid.1",
            seleccion=[],
            precios=PRECIOS,
            ttl_min=30,
            ahora=AHORA,
        )


def test_cantidad_debe_ser_positiva():
    with pytest.raises(ValueError):
        SeleccionLinea(sku="SKU-1", cantidad=0)


def test_id_es_determinista_e_independiente_del_orden():
    """Un reproceso del mismo mensaje tiene que dar el mismo id (invariante 5)."""
    a = [SeleccionLinea(sku="SKU-1", cantidad=2), SeleccionLinea(sku="SKU-2", cantidad=1)]
    b = [SeleccionLinea(sku="SKU-2", cantidad=1), SeleccionLinea(sku="SKU-1", cantidad=2)]
    assert id_propuesta("h", "wamid.1", a) == id_propuesta("h", "wamid.1", b)


def test_id_distingue_remitente_mensaje_y_carrito():
    base = [SeleccionLinea(sku="SKU-1", cantidad=1)]
    otro_carrito = [SeleccionLinea(sku="SKU-1", cantidad=2)]

    ids = {
        id_propuesta("h1", "wamid.1", base),
        id_propuesta("h2", "wamid.1", base),
        id_propuesta("h1", "wamid.2", base),
        id_propuesta("h1", "wamid.1", otro_carrito),
    }
    assert len(ids) == 4


def test_vencida_compara_contra_expira_at():
    p = construir_propuesta(
        wa_id_hash="h",
        wa_message_id="wamid.1",
        seleccion=[SeleccionLinea(sku="SKU-1", cantidad=1)],
        precios=PRECIOS,
        ttl_min=30,
        ahora=AHORA,
    )
    assert not p.vencida(AHORA + timedelta(minutes=29))
    assert p.vencida(AHORA + timedelta(minutes=30))
