"""Detección de sesión y persistencia, sin levantar un navegador.

`_esta_autenticado` se prueba con una página de mentira porque su caso
interesante no es el feliz: es qué hace cuando Playwright lanza en mitad de una
navegación. Eso ocurre justo en el instante que se intenta detectar, y montar un
Chromium para provocarlo cuesta más de lo que aporta.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from packages.adapters.prisa.sesion import Credenciales, Sesion, _esta_autenticado


class PaginaFalsa:
    def __init__(self, resultado: object = None, lanza: Exception | None = None) -> None:
        self._resultado = resultado
        self._lanza = lanza
        self.consultas: list[str] = []

    async def query_selector(self, selector: str) -> object:
        self.consultas.append(selector)
        if self._lanza is not None:
            raise self._lanza
        return self._resultado


async def test_detecta_la_sesion_por_el_enlace_de_salir() -> None:
    pagina = PaginaFalsa(resultado=object())

    assert await _esta_autenticado(pagina)
    assert "customer/user/logout" in pagina.consultas[0]


async def test_sin_enlace_de_salir_no_hay_sesion() -> None:
    assert not await _esta_autenticado(PaginaFalsa(resultado=None))


async def test_una_navegacion_en_curso_no_tumba_la_comprobacion() -> None:
    """El error que rompía `--manual` justo al acertar.

    Playwright lanza "Execution context was destroyed" cuando la página navega
    mientras se la consulta — que es exactamente lo que hace el sitio al
    completarse el login. La respuesta correcta es "todavía no consta", para que
    el sondeo vuelva a preguntar, no una excepción que aborta el comando.
    """
    pagina = PaginaFalsa(
        lanza=Exception("Execution context was destroyed, most likely because of a navigation")
    )

    assert await _esta_autenticado(pagina) is False


async def test_la_sesion_va_y_vuelve_de_json() -> None:
    original = Sesion(cookies={"OROSFID": "abc", "OCXS": "def"})

    recuperada = Sesion.de_json(original.a_json())

    assert recuperada.cookies == original.cookies
    assert recuperada.obtenida_en == original.obtenida_en


def test_la_sesion_se_guarda_solo_para_su_dueno(tmp_path: Path) -> None:
    """Estas cookies valen lo mismo que la contraseña mientras duren."""
    ruta = tmp_path / "sub" / "sesion.json"

    Sesion(cookies={"OROSFID": "abc"}).guardar(ruta)

    assert ruta.stat().st_mode & 0o777 == 0o600
    cargada = Sesion.cargar(ruta)
    assert cargada is not None
    assert cargada.cookies == {"OROSFID": "abc"}


def test_una_sesion_ilegible_no_revienta_el_arranque(tmp_path: Path) -> None:
    """Se vuelve a entrar, que es recuperable. Lanzar aquí no lo sería."""
    ruta = tmp_path / "rota.json"
    ruta.write_text("{ esto no es json", encoding="utf-8")

    assert Sesion.cargar(ruta) is None
    assert Sesion.cargar(tmp_path / "no-existe.json") is None


def test_la_caducidad_se_mide_desde_que_se_obtuvo() -> None:
    vieja = Sesion(cookies={}, obtenida_en=datetime.now(UTC) - timedelta(hours=9))
    nueva = Sesion(cookies={})

    assert vieja.caducada()
    assert not nueva.caducada()


def test_las_credenciales_no_admiten_vacios() -> None:
    """Un usuario vacío llega al sitio como un login anónimo y confunde."""
    with pytest.raises(ValueError, match="obligatorios"):
        Credenciales(usuario="", password="algo")
    with pytest.raises(ValueError, match="obligatorios"):
        Credenciales(usuario="alguien@ejemplo.cl", password="")


def test_el_repr_no_filtra_la_contrasena() -> None:
    """Los tracebacks acaban en los logs, y los logs se comparten."""
    texto = repr(Credenciales(usuario="alguien@ejemplo.cl", password="secreta-de-verdad"))

    assert "secreta-de-verdad" not in texto
    assert "alguien@ejemplo.cl" in texto
