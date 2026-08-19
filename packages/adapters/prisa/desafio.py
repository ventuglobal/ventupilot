"""Desafío JavaScript del WAF que protege prisa.cl.

Es la primera de las tres barreras del login, y la que hace que un `curl` o un
`httpx.get()` parezcan estar rotos sin estarlo: la primera petición a cualquier
URL del sitio no devuelve la página, sino ~750 bytes de HTML con un script que

1. descifra tres constantes hexadecimales con AES (la librería `slowAES`),
2. guarda el resultado en una cookie, y
3. recarga la misma URL con un parámetro añadido.

Un cliente sin JavaScript se queda en el paso 1 para siempre. No hay error, no
hay 403: se recibe un 200 con HTML que no contiene ni el formulario ni el
`_csrf_token`, y el fallo aparece mucho más abajo como "no encuentro el campo".

El descifrado no necesita navegador. `slowAES.decrypt(ct, 2, key, iv)` es
AES-128-CBC —el `2` es el modo CBC— sobre un único bloque de 16 bytes, y el
resultado se escribe en hexadecimal **sin quitar relleno**. Verificado contra
el propio `aes.min.js` del sitio; el vector del test viene de ahí.

Las tres constantes cambian en cada respuesta, así que hay que resolver el
desafío cada vez que aparece, no cachear la cookie y olvidarse.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# `toNumbers("...")` aparece tres veces y **el orden importa**: clave, IV y
# texto cifrado, en ese orden, tal como los pasa la llamada a `slowAES.decrypt`.
_RE_CONSTANTES = re.compile(r'toNumbers\("([0-9a-fA-F]+)"\)')
# El nombre de la cookie no se fija en código a propósito: el WAF lo rota
# (`OCXS` hoy) y hardcodearlo convierte una rotación en un bloqueo silencioso.
_RE_COOKIE = re.compile(r'document\.cookie\s*=\s*"([^"=]+)="')
_RE_DESTINO = re.compile(r'document\.location\.href\s*=\s*"([^"]+)"')

_BLOQUE = 16


@dataclass(frozen=True, slots=True)
class Desafio:
    """Un desafío resuelto: qué cookie poner y a dónde volver."""

    cookie: str
    valor: str
    destino: str


def es_desafio(html: str) -> bool:
    """True si la respuesta es la página del desafío y no contenido real.

    Se comprueba por la presencia del script, no por el tamaño: el WAF cambia
    el relleno del HTML y un umbral de bytes envejece mal.
    """
    return "slowAES.decrypt" in html


def resolver(html: str) -> Desafio | None:
    """Resuelve el desafío contenido en `html`.

    Returns:
        El `Desafio` con la cookie a instalar y la URL a la que volver, o None
        si `html` no es una página de desafío o su forma cambió.

    Devolver None en vez de lanzar es deliberado: si el WAF cambia el script,
    lo que hay que hacer es dejar que el navegador se encargue, no tumbar el
    proceso. Quien llama distingue "no era un desafío" de "no supe resolverlo"
    con `es_desafio`.
    """
    constantes = _RE_CONSTANTES.findall(html)
    if len(constantes) != 3:
        return None

    cookie = _RE_COOKIE.search(html)
    destino = _RE_DESTINO.search(html)
    if cookie is None or destino is None:
        return None

    try:
        clave, iv, cifrado = (bytes.fromhex(c) for c in constantes)
    except ValueError:
        return None

    if len(clave) != _BLOQUE or len(iv) != _BLOQUE or len(cifrado) % _BLOQUE:
        return None

    descifrador = Cipher(algorithms.AES(clave), modes.CBC(iv)).decryptor()
    claro = descifrador.update(cifrado) + descifrador.finalize()

    # Sin `unpad`: el script hace `toHex()` sobre el array completo que devuelve
    # `slowAES.decrypt`. Quitar relleno PKCS#7 aquí da una cookie más corta que
    # el WAF rechaza, y el síntoma es un bucle de desafíos que nunca converge.
    return Desafio(cookie=cookie.group(1), valor=claro.hex(), destino=destino.group(1))
