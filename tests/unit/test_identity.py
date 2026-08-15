from __future__ import annotations

import pytest

from packages.adapters.whatsapp.identity import enmascarar, hash_wa_id

PEPPER = "0" * 64
WA_ID = "56912345678"


def test_hash_es_estable() -> None:
    assert hash_wa_id(WA_ID, PEPPER) == hash_wa_id(WA_ID, PEPPER)


def test_numeros_distintos_dan_hashes_distintos() -> None:
    assert hash_wa_id(WA_ID, PEPPER) != hash_wa_id("56987654321", PEPPER)


def test_el_pepper_cambia_el_hash() -> None:
    """Rotar el pepper invalida los hashes almacenados: es parte del esquema."""
    assert hash_wa_id(WA_ID, PEPPER) != hash_wa_id(WA_ID, "1" * 64)


def test_pepper_vacio_falla_en_vez_de_degradar() -> None:
    with pytest.raises(ValueError, match="WA_ID_PEPPER"):
        hash_wa_id(WA_ID, "")


def test_hash_no_contiene_el_numero() -> None:
    assert WA_ID not in hash_wa_id(WA_ID, PEPPER)


def test_enmascarar_conserva_puntas() -> None:
    enmascarado = enmascarar(WA_ID)
    assert enmascarado.startswith("56")
    assert enmascarado.endswith("678")
    assert "9123" not in enmascarado


def test_enmascarar_numero_corto_no_filtra_nada() -> None:
    assert enmascarar("1234") == "••••"
