"""Navegación por prisa.cl con las cookies que dejó el login.

El navegador es caro —un Chromium por petición no se sostiene en un worker— y
solo hace falta para el reCAPTCHA del login. Una vez dentro, prisa.cl es un
sitio normal: cookie de sesión y HTML. Este cliente hace eso con `httpx`.

Lo único que sigue estorbando es el WAF: puede volver a plantear su desafío en
cualquier petición, no solo en la primera. Se resuelve de forma transparente
—`desafio.py` no necesita navegador— y por eso el desafío no aparece en la API
de este módulo: quien llama pide una ruta y recibe la página.
"""

from __future__ import annotations

import logging

import httpx

from .desafio import es_desafio, resolver
from .sesion import BASE_URL, RUTA_LOGOUT, Sesion

log = logging.getLogger("prisa.cliente")

# El WAF mira el User-Agent. Uno de librería HTTP se lleva desafío en cada
# petición, lo que funciona pero multiplica por dos el tráfico.
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Techo de desafíos encadenados por petición. Con uno basta en la práctica; dos
# cubre el caso de que el WAF re-desafíe tras el redirect. Más que eso es un
# bucle, y un bucle contra un WAF es la forma más rápida de que te bloqueen.
_MAX_DESAFIOS = 2


class SesionCaducadaPrisa(Exception):
    """La petición se resolvió como visitante anónimo: hay que volver a entrar."""


class ClientePrisa:
    """Cliente HTTP autenticado contra prisa.cl.

    No renueva la sesión por su cuenta a propósito: iniciar sesión abre un
    navegador y consume reputación de IP frente al reCAPTCHA. Quién decide
    reintentar, y cada cuánto, es del que orquesta, no de este objeto.
    """

    def __init__(
        self,
        sesion: Sesion,
        *,
        base_url: str = BASE_URL,
        cliente: httpx.AsyncClient | None = None,
        timeout: float = 20.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._sesion = sesion
        self._timeout = timeout
        self._cliente = cliente
        self._propio = cliente is None
        self._preparado = False

    async def _http(self) -> httpx.AsyncClient:
        if self._cliente is None:
            self._cliente = httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=True,
            )
        if not self._preparado:
            # Las cookies y el User-Agent se instalan aquí, y no solo al
            # construir el cliente propio, porque un cliente inyectado —el de
            # los tests, o uno compartido -- llegaría sin ellos y las peticiones
            # saldrían como visitante anónimo. El síntoma sería una sesión que
            # "caduca" nada más crearse.
            self._cliente.headers.setdefault("User-Agent", UA)
            for nombre, valor in self._sesion.cookies.items():
                self._cliente.cookies.set(nombre, valor)
            self._preparado = True
        return self._cliente

    async def cerrar(self) -> None:
        if self._cliente is not None and self._propio:
            await self._cliente.aclose()
            self._cliente = None

    async def obtener(self, ruta: str, *, ajax: bool = False) -> httpx.Response:
        """GET de una ruta del sitio, resolviendo el desafío del WAF si aparece.

        Args:
            ruta: ruta absoluta del sitio (`/customer/order/`) o URL completa.
            ajax: manda `X-Requested-With: XMLHttpRequest`. Los datagrids de
                OroCommerce —que son de donde salen los listados, porque en el
                HTML no están— devuelven la página entera sin esa cabecera y el
                JSON con ella. Sin esto se recibe un HTML de 2 MB y se concluye
                que el endpoint no sirve.

        Returns:
            La respuesta con el contenido real. Nunca la página del desafío.

        Raises:
            RuntimeError: si el WAF sigue desafiando tras `_MAX_DESAFIOS`
                intentos. Significa que cambió el script y hay que resolverlo
                con navegador.
        """
        cliente = await self._http()
        url = ruta if ruta.startswith("http") else f"{self._base_url}{ruta}"
        cabeceras = {"X-Requested-With": "XMLHttpRequest"} if ajax else None

        for intento in range(_MAX_DESAFIOS + 1):
            respuesta = await cliente.get(url, headers=cabeceras)
            if not es_desafio(respuesta.text):
                return respuesta

            desafio = resolver(respuesta.text)
            if desafio is None:
                raise RuntimeError(
                    "el WAF de prisa.cl planteó un desafío que no supe resolver; "
                    "el script cambió y hace falta revisar desafio.py"
                )

            log.debug("desafío del WAF resuelto (intento %d)", intento + 1)
            # La cookie va al jar del cliente, no a esta petición suelta: el
            # WAF la exige también en las siguientes.
            cliente.cookies.set(desafio.cookie, desafio.valor, domain=_dominio(url))
            url = desafio.destino

        raise RuntimeError(
            f"el WAF de prisa.cl siguió desafiando tras {_MAX_DESAFIOS} intentos"
        )

    async def esta_viva(self) -> bool:
        """¿La sesión sigue autenticada?

        Es la única respuesta fiable sobre la vigencia: OroCommerce caduca por
        inactividad y no publica ese plazo, así que `Sesion.caducada()` es una
        heurística barata y esto es la verdad.
        """
        try:
            respuesta = await self.obtener("/")
        except (httpx.HTTPError, RuntimeError):
            return False
        return RUTA_LOGOUT in respuesta.text


def _dominio(url: str) -> str:
    return httpx.URL(url).host
