"""El canario que distingue el catálogo público de los datos de la cuenta.

Medido contra prisa.cl: sin sesión, `/datagrid/frontend-product-search-grid`
responde `200` con JSON, 9.825 registros y las mismas 25 columnas que con
sesión —incluida `has_price`—. Lo único que cambia es que los precios vienen
vacíos. Una extracción anónima, por tanto, se ve exitosa: filas, columnas y
total cuadran. Estos tests fijan la única señal que las separa.
"""

from __future__ import annotations

import pytest

from scripts.prisa_ver import _canario_precios


def _fila(has_price: object) -> dict[str, object]:
    return {"id": 1, "name": "resma", "has_price": has_price, "minimal_price": None}


def test_avisa_cuando_ninguna_fila_trae_precio(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Es el caso real de una sesión caducada, y el que no se puede pasar por alto."""
    _canario_precios([_fila("") for _ in range(20)])

    salida = capsys.readouterr().out
    assert "con precio  0 de 20 filas" in salida
    assert "catálogo público" in salida


def test_no_avisa_cuando_los_precios_estan(capsys: pytest.CaptureFixture[str]) -> None:
    _canario_precios([_fila(True) for _ in range(20)])

    salida = capsys.readouterr().out
    assert "con precio  20 de 20 filas" in salida
    assert "catálogo público" not in salida


@pytest.mark.parametrize("vacio", ["", None, False, "false", 0])
def test_las_formas_de_no_tener_precio_cuentan_como_no(
    vacio: object, capsys: pytest.CaptureFixture[str]
) -> None:
    """Oro no es consistente: el mismo "sin precio" llega de varias formas.

    Si alguna se colara como verdadera, el canario cantaría éxito justo cuando
    no hay nada que extraer, que es el fallo que este módulo existe para evitar.
    """
    _canario_precios([_fila(vacio)])

    assert "con precio  0 de 1 filas" in capsys.readouterr().out


def test_no_cuenta_lo_que_no_es_una_fila(capsys: pytest.CaptureFixture[str]) -> None:
    """El datagrid puede traer filas que no son objetos; no debe reventar aquí."""
    _canario_precios(["", None, 3])

    assert "con precio  0 de 3 filas" in capsys.readouterr().out


def test_callado_si_no_hay_filas(capsys: pytest.CaptureFixture[str]) -> None:
    """Sin filas no hay nada que decir sobre precios: lo raro es el cero de filas."""
    _canario_precios([])

    assert capsys.readouterr().out == ""
