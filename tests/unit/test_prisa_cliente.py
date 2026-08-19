"""El cliente HTTP de prisa.cl atraviesa el WAF sin que quien llama se entere.

Todo con `MockTransport`: estos tests describen el protocolo, no el sitio. Si
prisa.cl cambia, lo que hay que actualizar es `scripts/prisa_login.py` contra
el sitio real, no esto.
"""

from __future__ import annotations

import httpx
import pytest

from packages.adapters.prisa import ClientePrisa, Sesion
from tests.unit.test_prisa_desafio import HTML_DESAFIO, OCXS_ESPERADO

PAGINA = '<html><body><a href="/customer/user/logout">Salir</a>ok</body></html>'
ANONIMA = '<html><body><a href="/customer/user/login">Entrar</a></body></html>'


def _cliente(manejador: object) -> ClientePrisa:
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(manejador),  # type: ignore[arg-type]
        follow_redirects=True,
    )
    return ClientePrisa(Sesion(cookies={"OROSFID": "abc"}), cliente=http)


async def test_devuelve_la_pagina_cuando_no_hay_desafio() -> None:
    def manejador(peticion: httpx.Request) -> httpx.Response:
        assert peticion.headers["cookie"].startswith("OROSFID=abc")
        return httpx.Response(200, text=PAGINA)

    cliente = _cliente(manejador)
    respuesta = await cliente.obtener("/customer/order/")

    assert "ok" in respuesta.text


async def test_resuelve_el_desafio_y_reintenta_en_el_destino() -> None:
    vistas: list[str] = []

    def manejador(peticion: httpx.Request) -> httpx.Response:
        vistas.append(str(peticion.url))
        if len(vistas) == 1:
            return httpx.Response(200, text=HTML_DESAFIO)
        # La cookie del desafío tiene que viajar ya en la segunda petición.
        assert f"OCXS={OCXS_ESPERADO}" in peticion.headers["cookie"]
        return httpx.Response(200, text=PAGINA)

    cliente = _cliente(manejador)
    respuesta = await cliente.obtener("/customer/order/")

    assert "ok" in respuesta.text
    assert vistas[0] == "https://www.prisa.cl/customer/order/"
    # El reintento va a la URL que pide el script, no a la original: el WAF
    # espera el parámetro que él mismo añadió.
    assert vistas[1] == "https://www.prisa.cl/?0563b747a007865826ca8e670d1adcba=1"


async def test_la_cookie_del_desafio_queda_para_las_siguientes() -> None:
    """Si hubiera que resolverlo en cada petición, se duplicaría el tráfico."""
    desafios = 0

    def manejador(peticion: httpx.Request) -> httpx.Response:
        nonlocal desafios
        if "OCXS" not in peticion.headers.get("cookie", ""):
            desafios += 1
            return httpx.Response(200, text=HTML_DESAFIO)
        return httpx.Response(200, text=PAGINA)

    cliente = _cliente(manejador)
    await cliente.obtener("/uno")
    await cliente.obtener("/dos")

    assert desafios == 1


async def test_se_rinde_en_vez_de_martillear_al_waf() -> None:
    """Un bucle contra un WAF es la vía rápida a un bloqueo por IP."""
    intentos = 0

    def manejador(peticion: httpx.Request) -> httpx.Response:
        nonlocal intentos
        intentos += 1
        return httpx.Response(200, text=HTML_DESAFIO)

    cliente = _cliente(manejador)
    with pytest.raises(RuntimeError, match="siguió desafiando"):
        await cliente.obtener("/")

    assert intentos == 3  # el original más los dos reintentos permitidos


async def test_falla_claro_si_el_script_del_waf_cambia() -> None:
    roto = HTML_DESAFIO.replace("2e456ca25d1adc12dfb564fff6d1cb1e", "xx")

    def manejador(peticion: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=roto)

    cliente = _cliente(manejador)
    with pytest.raises(RuntimeError, match="no supe resolver"):
        await cliente.obtener("/")


async def test_esta_viva_distingue_sesion_de_visitante_anonimo() -> None:
    """La cookie de sesión existe también sin autenticar: no sirve de señal."""
    cliente = _cliente(lambda _: httpx.Response(200, text=PAGINA))
    assert await cliente.esta_viva()

    cliente = _cliente(lambda _: httpx.Response(200, text=ANONIMA))
    assert not await cliente.esta_viva()


async def test_esta_viva_no_lanza_si_la_red_falla() -> None:
    def manejador(peticion: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sin red")

    assert not await _cliente(manejador).esta_viva()


async def test_ajax_manda_la_cabecera_que_el_datagrid_exige() -> None:
    """Sin ella Oro devuelve la página entera en vez del JSON.

    El síntoma es un HTML de 2 MB donde se esperaban filas, que se lee como
    "este endpoint no sirve" cuando lo único que falta es una cabecera.
    """
    vistas: list[str | None] = []

    def manejador(peticion: httpx.Request) -> httpx.Response:
        vistas.append(peticion.headers.get("x-requested-with"))
        return httpx.Response(200, text='{"data": []}')

    cliente = _cliente(manejador)
    await cliente.obtener("/datagrid/frontend-product-search-grid", ajax=True)
    await cliente.obtener("/customer/order/")

    assert vistas == ["XMLHttpRequest", None]


async def test_la_cabecera_ajax_sobrevive_al_desafio_del_waf() -> None:
    """El reintento tras el desafío es la petición que de verdad trae los datos.

    Si la cabecera se quedara en el primer intento, el datagrid contestaría con
    HTML justo cuando por fin se le puede preguntar.
    """
    vistas: list[str | None] = []

    def manejador(peticion: httpx.Request) -> httpx.Response:
        vistas.append(peticion.headers.get("x-requested-with"))
        if len(vistas) == 1:
            return httpx.Response(200, text=HTML_DESAFIO)
        return httpx.Response(200, text='{"data": []}')

    cliente = _cliente(manejador)
    await cliente.obtener("/datagrid/frontend-product-search-grid", ajax=True)

    assert vistas == ["XMLHttpRequest", "XMLHttpRequest"]
