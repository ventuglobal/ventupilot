"""El desafío del WAF de prisa.cl, resuelto sin navegador.

El vector no es inventado: es una respuesta real del sitio, y el valor esperado
se obtuvo ejecutando el propio `aes.min.js` de prisa.cl sobre esas mismas
constantes. Si alguien "arregla" `desafio.py` quitando relleno PKCS#7 —que es
lo que uno hace por instinto al ver AES-CBC—, este test se pone rojo. Sin él,
el síntoma sería un bucle de desafíos en producción.
"""

from __future__ import annotations

from packages.adapters.prisa.desafio import es_desafio, resolver

# El HTML del desafío es JavaScript minificado de un tercero: se reproduce tal
# cual, porque recortarlo para que quepa en 100 columnas dejaría de ser el
# vector real que hace útil a este test.
# ruff: noqa: E501

# Respuesta real de https://www.prisa.cl/ ante un cliente sin cookie.
HTML_DESAFIO = (
    '<html><body><script type="text/javascript" src="/aes.min.js"></script><script>'
    "function toNumbers(d){var e=[];d.replace(/(..)/g,function(d){e.push(parseInt(d,16))});return e}"
    'function toHex(){for(var d=[],d=1==arguments.length&&arguments[0].constructor==Array?arguments[0]:arguments,e="",f=0;f<d.length;f++)e+=(16>d[f]?"0":"")+d[f].toString(16);return e.toLowerCase()}'
    'var a=toNumbers("2ab351f0c815e22c9fe7acca4e6e2174"),'
    'b=toNumbers("2e456ca25d1adc12dfb564fff6d1cb1e"),'
    'c=toNumbers("4d73a081215fbd0b70ce1845b22469dc");'
    'document.cookie="OCXS="+toHex(slowAES.decrypt(c,2,a,b))+"; SameSite=None; Secure; '
    'expires=Thu, 31-Dec-37 23:55:55 GMT; path=/";'
    'document.location.href="https://www.prisa.cl/?0563b747a007865826ca8e670d1adcba=1";'
    "</script></body></html>"
)

# Lo que devuelve slowAES.decrypt(c, 2, a, b) ejecutado en el navegador.
OCXS_ESPERADO = "b6f2863e74e75c9db181e0a6c5edcfd1"


def test_reconoce_la_pagina_del_desafio() -> None:
    assert es_desafio(HTML_DESAFIO)


def test_no_confunde_html_normal_con_un_desafio() -> None:
    assert not es_desafio("<html><body><form id='form-login'></form></body></html>")


def test_resuelve_el_desafio_como_lo_haria_el_navegador() -> None:
    desafio = resolver(HTML_DESAFIO)

    assert desafio is not None
    assert desafio.cookie == "OCXS"
    assert desafio.valor == OCXS_ESPERADO
    assert desafio.destino == "https://www.prisa.cl/?0563b747a007865826ca8e670d1adcba=1"


def test_el_valor_conserva_el_bloque_entero() -> None:
    """16 bytes, no 12.

    `slowAES.decrypt` no quita relleno: el script hace `toHex()` sobre el array
    completo. Una cookie recortada la rechaza el WAF y vuelve a desafiar, lo
    que desde fuera parece que el descifrado "no funciona".
    """
    desafio = resolver(HTML_DESAFIO)

    assert desafio is not None
    assert len(bytes.fromhex(desafio.valor)) == 16


def test_devuelve_none_si_el_script_cambio() -> None:
    """Sin las tres constantes no hay nada que descifrar.

    Se devuelve None en vez de lanzar para que quien llama pueda caer al
    navegador, que sigue sabiendo resolverlo sea cual sea el script.
    """
    assert resolver(HTML_DESAFIO.replace('b=toNumbers("2e456ca25d1adc12dfb564fff6d1cb1e"),', "")) is None
    assert resolver("<html><body>hola</body></html>") is None


def test_devuelve_none_si_falta_el_destino() -> None:
    assert resolver(HTML_DESAFIO.replace("document.location.href=", "var x=")) is None


def test_no_fija_el_nombre_de_la_cookie() -> None:
    """El WAF rota el nombre. Hardcodear `OCXS` lo convertiría en un bloqueo."""
    desafio = resolver(HTML_DESAFIO.replace('document.cookie="OCXS="', 'document.cookie="ZZQ="'))

    assert desafio is not None
    assert desafio.cookie == "ZZQ"
    assert desafio.valor == OCXS_ESPERADO
