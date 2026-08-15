"""Tests del alta declarativa de remitentes.

Esto concede permisos, así que los casos que importan no son los felices sino
los torcidos: una errata en el panel de Railway no debe conceder de más ni
tumbar el arranque del worker.
"""

from __future__ import annotations

from packages.adapters.altas import normalizar_numero, parsear
from packages.domain.identidad import Permiso


def test_normaliza_el_formato_humano():
    """Meta manda el wa_id solo con dígitos; un `+` de más rompe el hash."""
    assert normalizar_numero("+56 9 6626-6451") == "56966266451"
    assert normalizar_numero("56966266451") == "56966266451"


def test_parsea_una_entrada_con_permisos():
    assert parsear("+56 9 6626 6451:consultar,cotizar") == [
        ("56966266451", frozenset({Permiso.CONSULTAR, Permiso.COTIZAR}))
    ]


def test_parsea_varias_entradas():
    r = dict(parsear("56911112222:consultar; 56922223333:consultar,cotizar,pedir"))
    assert r["56911112222"] == frozenset({Permiso.CONSULTAR})
    assert Permiso.PEDIR in r["56922223333"]


def test_sin_permisos_concede_solo_consultar():
    """El defecto tiene que ser el mínimo, nunca el máximo."""
    assert parsear("56911112222") == [("56911112222", frozenset({Permiso.CONSULTAR}))]
    assert parsear("56911112222:") == [("56911112222", frozenset({Permiso.CONSULTAR}))]


def test_un_permiso_desconocido_no_se_concede_ni_revienta():
    """Una errata no debe conceder nada raro ni impedir que el worker arranque."""
    numero, permisos = parsear("56911112222:consultar,superusuario")[0]
    assert numero == "56911112222"
    assert permisos == frozenset({Permiso.CONSULTAR})


def test_tolera_espacios_mayusculas_y_entradas_vacias():
    """Esto se edita a mano en un panel web."""
    assert parsear("  ;; 56911112222 : CONSULTAR , Cotizar ;  ") == [
        ("56911112222", frozenset({Permiso.CONSULTAR, Permiso.COTIZAR}))
    ]


def test_entrada_sin_digitos_se_ignora():
    assert parsear("no-es-un-numero:consultar") == []


def test_declaracion_vacia_no_da_de_alta_a_nadie():
    assert parsear("") == []
    assert parsear("   ") == []
