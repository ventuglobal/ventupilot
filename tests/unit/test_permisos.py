"""Tests de la invariante 4: el teléfono identifica, no autoriza."""

from __future__ import annotations

from packages.domain.identidad import DESCONOCIDO, ClienteAutorizado, Permiso


def test_desconocido_no_puede_nada():
    """Quien escribe por primera vez no hereda ningún permiso."""
    for permiso in Permiso:
        assert not DESCONOCIDO.puede(permiso)


def test_permisos_son_independientes():
    """Poder cotizar no implica poder comprometer un pedido."""
    c = ClienteAutorizado(
        wa_id_hash="h",
        permisos=frozenset({Permiso.CONSULTAR, Permiso.COTIZAR}),
    )
    assert c.puede(Permiso.CONSULTAR)
    assert c.puede(Permiso.COTIZAR)
    assert not c.puede(Permiso.PEDIR)


def test_inactivo_corta_todo_sin_perder_los_permisos():
    c = ClienteAutorizado(
        wa_id_hash="h",
        permisos=frozenset({Permiso.CONSULTAR, Permiso.COTIZAR, Permiso.PEDIR}),
        activo=False,
    )
    for permiso in Permiso:
        assert not c.puede(permiso)
    # Se conservan para cuando se reactive.
    assert Permiso.PEDIR in c.permisos
