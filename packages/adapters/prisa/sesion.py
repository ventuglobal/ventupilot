"""Login en prisa.cl y la sesión que deja detrás.

prisa.cl corre **OroCommerce** (B2B) detrás de un WAF con desafío JavaScript.
El login tiene tres barreras encadenadas, y fallar cualquiera de ellas produce
el mismo síntoma inútil —una página que no es la que se pedía—, que es por lo
que reproducirlo con `requests`/`httpx` a mano no sale:

1. **Desafío JS del WAF.** Ver `desafio.py`. Sin resolverlo no se llega ni al
   formulario.
2. **CSRF de Symfony.** Hay que hacer GET de `/customer/user/login` para
   obtener el `_csrf_token` y la cookie `https-_csrf` que lo acompaña, y el
   POST va a `/customer/user/login-check` — con **guion**, no `login_check`,
   que es el valor por defecto de Oro y el que uno escribe de memoria.
3. **reCAPTCHA v2 invisible.** El formulario incluye `g-recaptcha-response`.
   Ese token solo lo puede generar JavaScript ejecutándose en un navegador
   real, y es la barrera que hace imposible el login con un cliente HTTP puro.

Por (3) este módulo usa un navegador. Comprobado contra el sitio: un Chromium
headless normal pasa las tres barreras sin ayuda —el reCAPTCHA invisible no
levanta reto— y el POST llega al nivel de aplicación. No hace falta ningún
servicio de resolución de captchas.

Lo que el navegador deja es un puñado de cookies (`OROSFID` es la de sesión).
A partir de ahí `cliente.py` navega con `httpx`, que es mucho más barato que
mantener un Chromium vivo. El navegador es para entrar, no para quedarse.

Si el reCAPTCHA empieza a levantar retos —lo hace en función de la reputación
de la IP—, la salida no es cambiar de código sino de IP: `proxy` acepta un
proxy residencial chileno (el de Apify, por ejemplo). Ver PRISA.md.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger("prisa.sesion")

BASE_URL = "https://www.prisa.cl"
RUTA_LOGIN = "/customer/user/login"
RUTA_LOGOUT = "/customer/user/logout"

# Selectores del formulario. Los `name` de Symfony (`_username`, `_password`)
# son estables entre versiones de Oro; los `id` los pone la plantilla de Prisa.
# Se prefiere el `name`, que es lo que el backend lee de verdad.
_CAMPO_USUARIO = 'form#form-login input[name="_username"]'
_CAMPO_PASSWORD = 'form#form-login input[name="_password"]'  # noqa: S105 - selector CSS
_BOTON_ENTRAR = 'form#form-login button[type="submit"], form#form-login #start_login'

# El error se pinta en un bloque de alerta de Oro. El selector va deliberadamente
# ancho —por subcadena de clase, no por clase exacta— porque la plantilla de
# Prisa las mezcla y afinarlo deja el diagnóstico vacío justo cuando hace falta:
# "no autenticó" a secas no distingue una contraseña mal puesta de una cuenta
# deshabilitada, y solo la primera se arregla desde aquí.
_SELECTOR_ALERTA = "[class*='alert'], [class*='error'], [class*='notification']"

# Vida útil por defecto de la sesión. OroCommerce caduca por inactividad, así
# que esto es un techo prudente, no el valor real del servidor: la verdad la
# da `esta_viva()`, que pregunta al sitio.
TTL_POR_DEFECTO = timedelta(hours=8)


class ErrorLoginPrisa(Exception):
    """El login no llegó a completarse.

    `mensaje_sitio` lleva el texto que mostró prisa.cl cuando lo hubo. Es la
    diferencia entre "la contraseña está mal" y "la cuenta está deshabilitada",
    y solo la primera se arregla cambiando la configuración.
    """

    def __init__(self, mensaje: str, *, mensaje_sitio: str = "") -> None:
        super().__init__(mensaje)
        self.mensaje_sitio = mensaje_sitio


@dataclass(frozen=True, slots=True)
class Credenciales:
    """Usuario y contraseña de prisa.cl.

    `usuario` es RUT con guion y dígito verificador (`12345678-9`) o correo: el
    formulario acepta ambos y el sitio los normaliza del lado del cliente.
    """

    usuario: str
    password: str

    def __post_init__(self) -> None:
        if not self.usuario or not self.password:
            raise ValueError("PRISA_USUARIO y PRISA_PASSWORD son obligatorios")

    def __repr__(self) -> str:
        # Que un traceback no vuelque la contraseña. Los tracebacks acaban en
        # los logs, y los logs se comparten.
        return f"Credenciales(usuario={self.usuario!r}, password=***)"


@dataclass(frozen=True, slots=True)
class Sesion:
    """Cookies de una sesión autenticada, con su fecha de obtención."""

    cookies: dict[str, str] = field(default_factory=dict)
    obtenida_en: datetime = field(default_factory=lambda: datetime.now(UTC))

    def caducada(self, ttl: timedelta = TTL_POR_DEFECTO) -> bool:
        return datetime.now(UTC) - self.obtenida_en >= ttl

    def a_json(self) -> str:
        return json.dumps(
            {"cookies": self.cookies, "obtenida_en": self.obtenida_en.isoformat()},
            ensure_ascii=False,
        )

    @classmethod
    def de_json(cls, texto: str) -> Sesion:
        datos = json.loads(texto)
        return cls(
            cookies=dict(datos["cookies"]),
            obtenida_en=datetime.fromisoformat(datos["obtenida_en"]),
        )

    def guardar(self, ruta: Path) -> None:
        """Persiste la sesión en disco con permisos 0600.

        Estas cookies son equivalentes a la contraseña mientras duren, así que
        el fichero se crea restringido **antes** de escribir, no después: entre
        un `write_text` y un `chmod` hay una ventana en la que cualquiera del
        sistema puede leerlas.
        """
        ruta.parent.mkdir(parents=True, exist_ok=True)
        with open(ruta, "w", encoding="utf-8", opener=_solo_dueno) as fh:
            fh.write(self.a_json())

    @classmethod
    def cargar(cls, ruta: Path) -> Sesion | None:
        """Lee una sesión guardada. None si no existe o está corrupta."""
        try:
            return cls.de_json(ruta.read_text(encoding="utf-8"))
        except (OSError, KeyError, ValueError):
            return None


def _solo_dueno(ruta: str, flags: int) -> int:
    import os

    return os.open(ruta, flags, 0o600)


async def iniciar_sesion(
    credenciales: Credenciales,
    *,
    base_url: str = BASE_URL,
    headless: bool = True,
    proxy: str = "",
    args_chromium: tuple[str, ...] = (),
    ejecutable: str = "",
    timeout_ms: int = 45_000,
) -> Sesion:
    """Entra en prisa.cl con un navegador y devuelve las cookies resultantes.

    Args:
        credenciales: RUT o correo, y contraseña.
        base_url: raíz del sitio. Parametrizada para poder apuntar a un entorno
            de pruebas sin tocar el código.
        headless: False abre ventana. Útil para ver qué pasa cuando el sitio
            cambia el formulario.
        proxy: proxy de salida, p. ej. un residencial chileno. Vacío = directo.
        args_chromium: banderas extra para Chromium. Existe por los entornos
            con proxy que rompe TLS; ver PRISA.md.
        ejecutable: ruta a un Chromium ya instalado. Vacío = el que descargó
            Playwright. Sirve en imágenes que traen su propio navegador y para
            no fallar cuando la revisión instalada no es la que espera esta
            versión de Playwright.
        timeout_ms: techo por navegación.

    Raises:
        ErrorLoginPrisa: si el sitio rechazó las credenciales, si mostró un
            error, o si tras enviar el formulario seguimos sin sesión.
        RuntimeError: si Playwright no está instalado.
    """
    try:
        from playwright.async_api import async_playwright
    except ModuleNotFoundError as exc:  # pragma: no cover - depende del entorno
        raise RuntimeError(
            "Falta Playwright. Instálalo con:\n"
            "  uv sync --extra prisa && uv run playwright install chromium"
        ) from exc

    lanzamiento: dict[str, Any] = {
        "headless": headless,
        "args": ["--disable-dev-shm-usage", *args_chromium],
    }
    if proxy:
        lanzamiento["proxy"] = {"server": proxy}
    if ejecutable:
        lanzamiento["executable_path"] = ejecutable

    async with async_playwright() as pw:
        navegador = await pw.chromium.launch(**lanzamiento)
        try:
            # `locale` y `timezone_id` chilenos no son cosmética: forman parte
            # de la señal que mira el reCAPTCHA, y un navegador que dice estar
            # en UTC pidiendo un sitio chileno puntúa peor.
            contexto = await navegador.new_context(
                locale="es-CL",
                timezone_id="America/Santiago",
            )
            pagina = await contexto.new_page()
            pagina.set_default_timeout(timeout_ms)

            # `domcontentloaded` y no `networkidle`: la home carga analítica y
            # widgets que no callan nunca, y esperar a que lo hagan es esperar
            # al timeout.
            await pagina.goto(f"{base_url}{RUTA_LOGIN}", wait_until="domcontentloaded")

            # El reCAPTCHA invisible necesita haberse inicializado antes del
            # envío. Sin esta espera el POST sale sin `g-recaptcha-response`.
            await pagina.wait_for_selector(_CAMPO_PASSWORD)
            await pagina.wait_for_timeout(3_000)

            await pagina.fill(_CAMPO_USUARIO, credenciales.usuario)
            await pagina.fill(_CAMPO_PASSWORD, credenciales.password)
            await pagina.click(_BOTON_ENTRAR)

            # Oro envía el login por AJAX y redirige después con JavaScript, así
            # que no hay una navegación única a la que engancharse. Se espera a
            # que el sitio se estabilice y se pregunta por el estado real.
            await pagina.wait_for_timeout(6_000)

            cookies = {c["name"]: c["value"] for c in await contexto.cookies()}
            autenticado = await _esta_autenticado(pagina)

            if not autenticado:
                aviso = await _mensaje_del_sitio(pagina)
                log.warning(
                    "login rechazado en prisa.cl para %s", _enmascarar(credenciales.usuario)
                )
                raise ErrorLoginPrisa(
                    f"prisa.cl no autenticó a {_enmascarar(credenciales.usuario)}"
                    + (f": {aviso}" if aviso else ""),
                    mensaje_sitio=aviso,
                )

            log.info("sesión de prisa.cl iniciada para %s", _enmascarar(credenciales.usuario))
            return Sesion(cookies=cookies)
        finally:
            await navegador.close()


async def _esta_autenticado(pagina: Any) -> bool:
    """¿La página que tenemos delante es la de alguien con sesión?

    Se busca el enlace de salir en vez de mirar cookies: `OROSFID` existe
    también para un visitante anónimo, así que su presencia no dice nada.
    """
    return bool(await pagina.query_selector(f'a[href*="{RUTA_LOGOUT}"]'))


async def _mensaje_del_sitio(pagina: Any) -> str:
    """Recoge el aviso que pintó prisa.cl, si lo hubo."""
    try:
        textos = await pagina.eval_on_selector_all(
            _SELECTOR_ALERTA,
            "es => es.map(e => (e.textContent || '').trim().replace(/\\s+/g, ' '))",
        )
    except Exception:  # pragma: no cover - la página puede haberse ido
        return ""
    # Los contenedores anidan: el de fuera trae el aviso más el resto de la
    # página, y el de dentro solo el aviso. Se busca el más corto que aún diga
    # algo, que es el que lleva el texto y no el envoltorio.
    utiles = [str(t) for t in textos if t and 8 < len(str(t)) < 300]
    return min(utiles, key=len) if utiles else ""


def _enmascarar(usuario: str) -> str:
    """Deja el usuario reconocible en un log sin publicarlo entero.

    Un RUT es un identificador personal: la invariante 8 del proyecto prohíbe
    que aparezca completo en los logs.
    """
    if "@" in usuario:
        nombre, _, dominio = usuario.partition("@")
        return f"{nombre[:2]}***@{dominio}"
    return f"{usuario[:3]}***{usuario[-1:]}" if len(usuario) > 4 else "***"
