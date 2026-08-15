"""Tests de la política de acceso abierto.

Esto concede permisos a desconocidos, así que lo que hay que fijar con tests
son sus límites: qué concede, qué no, y a quién no debe alcanzar.
"""

from __future__ import annotations

from packages.agents.handler import permisos_abiertos
from packages.domain.identidad import DESCONOCIDO, ClienteAutorizado, Permiso


def test_por_defecto_concede_consultar_y_cotizar():
    assert permisos_abiertos("consultar,cotizar") == frozenset(
        {Permiso.CONSULTAR, Permiso.COTIZAR}
    )


def test_pedir_solo_si_se_pide_explicitamente():
    """`pedir` es el permiso con impacto económico: nunca por defecto."""
    assert Permiso.PEDIR not in permisos_abiertos("consultar,cotizar")
    assert Permiso.PEDIR in permisos_abiertos("consultar,cotizar,pedir")


def test_una_errata_no_deja_el_numero_mudo():
    """Un valor inválido se ignora; el resto sigue valiendo."""
    assert permisos_abiertos("consultar,superusuario") == frozenset({Permiso.CONSULTAR})


def test_sin_nada_valido_cae_a_consultar():
    assert permisos_abiertos("") == frozenset({Permiso.CONSULTAR})
    assert permisos_abiertos("basura") == frozenset({Permiso.CONSULTAR})


def test_tolera_espacios_y_mayusculas():
    assert permisos_abiertos(" CONSULTAR , Cotizar ") == frozenset(
        {Permiso.CONSULTAR, Permiso.COTIZAR}
    )


# ── A quién alcanza ──────────────────────────────────────────────────────────


def test_el_desconocido_no_esta_registrado():
    """Es la condición que dispara el acceso abierto."""
    assert not DESCONOCIDO.registrado


def test_un_desactivado_si_esta_registrado():
    """Y por eso el acceso abierto no lo alcanza.

    Desactivar a alguien es un acto deliberado del operador; abrir el canal
    no debe deshacerlo por un efecto lateral.
    """
    inactivo = ClienteAutorizado(wa_id_hash="h", permisos=frozenset(), activo=False)
    assert inactivo.registrado
    assert not inactivo.puede(Permiso.CONSULTAR)
