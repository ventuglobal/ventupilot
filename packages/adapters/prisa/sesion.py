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
import os
import shutil
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
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
# Marcarlo hace que Symfony emita su token persistente. Es lo que permite
# refrescar la sesión SIN navegador cuando caduca, que es la diferencia entre un
# worker desatendido y uno que llama a una persona cada ocho horas.
_CASILLA_RECORDARME = 'form#form-login input[name="_remember_me"]'

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

    `captura` es la ruta de una foto de la pantalla en el momento del fallo.
    Existe porque el sitio no siempre pinta un aviso: cuando no lo hace, "no
    autenticó" a secas no dice nada y la única forma de avanzar es mirar. Con la
    foto se mira una vez, en vez de volver a correrlo con ventana a ver qué pasa.
    """

    def __init__(
        self, mensaje: str, *, mensaje_sitio: str = "", captura: Path | None = None
    ) -> None:
        super().__init__(mensaje)
        self.mensaje_sitio = mensaje_sitio
        self.captura = captura


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


@contextmanager
def _pantalla() -> Iterator[None]:
    """Garantiza un DISPLAY donde abrir el navegador, levantando Xvfb si hace falta.

    Es el mismo camino que usa el bot de boletas del SII en ventu 1.0
    (`boleta_bot/auth.py::_pantalla`), y por la misma razón: hay logins que **no
    pasan en headless** —los antibot lo detectan— pero sí con un navegador con
    ventana. Xvfb fabrica esa ventana en un servidor sin monitor, así que la
    renovación queda automática por el MISMO camino que ya sabemos que funciona,
    sin depender de que haya una persona para abrirla.

    En una Mac o si ya hay `DISPLAY`, no hace nada.
    """
    if sys.platform in ("darwin", "win32") or os.environ.get("DISPLAY"):
        yield
        return

    if shutil.which("Xvfb") is None:
        raise RuntimeError(
            "No hay pantalla ni Xvfb, y el login de prisa.cl no pasa en headless. "
            "Instala xvfb en la imagen, o corre con headless=True si el perfil "
            "persistente ya trae sesión."
        )

    # El número sale del PID: dos procesos del mismo contenedor no se pisan y no
    # hay que coordinar nada entre ellos.
    numero = 90 + (os.getpid() % 9)
    proceso = subprocess.Popen(  # noqa: S603 - binario fijo, sin entrada del usuario
        ["Xvfb", f":{numero}", "-screen", "0", "1280x1024x24", "-nolisten", "tcp"],  # noqa: S607
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    previo = os.environ.get("DISPLAY")
    os.environ["DISPLAY"] = f":{numero}"
    time.sleep(1.5)  # margen para que Xvfb acepte conexiones
    log.info("pantalla virtual Xvfb en :%s", numero)
    try:
        yield
    finally:
        if previo is None:
            os.environ.pop("DISPLAY", None)
        else:
            os.environ["DISPLAY"] = previo
        proceso.terminate()
        try:
            proceso.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proceso.kill()


@asynccontextmanager
async def _navegador(
    *,
    headless: bool,
    proxy: str,
    args_chromium: tuple[str, ...],
    ejecutable: str,
    perfil: Path | None = None,
) -> AsyncIterator[Any]:
    """Abre Chromium y cede un contexto ya configurado para prisa.cl.

    Compartido por el login automático y el manual para que no se separen: el
    `locale` y la zona horaria chilenos no son cosmética —forman parte de la
    señal que mira el reCAPTCHA, y un navegador que dice estar en UTC pidiendo
    un sitio chileno puntúa peor—, y tenerlos duplicados es tenerlos distintos
    dentro de tres meses.

    Con `perfil` se usa un contexto **persistente**: el directorio guarda las
    cookies y el historial entre corridas, y eso es lo que hace que el reCAPTCHA
    deje de tratar cada login como un visitante recién llegado. Es lo que hace
    `prisa_b2b` en ventu 1.0, y la razón de que allí el headless funcione — pero
    solo *después* de que un login con ventana haya sembrado el perfil.
    """
    try:
        from playwright.async_api import async_playwright
    except ModuleNotFoundError as exc:  # pragma: no cover - depende del entorno
        raise RuntimeError(
            "Falta Playwright. Instálalo con:\n"
            "  uv sync --extra prisa && uv run playwright install chromium"
        ) from exc

    opciones: dict[str, Any] = {
        "headless": headless,
        "args": ["--disable-dev-shm-usage", *args_chromium],
    }
    if proxy:
        opciones["proxy"] = {"server": proxy}
    if ejecutable:
        opciones["executable_path"] = ejecutable

    contexto_pantalla = _pantalla() if not headless else _sin_pantalla()
    with contexto_pantalla:
        async with async_playwright() as pw:
            if perfil is not None:
                perfil.mkdir(parents=True, exist_ok=True)
                contexto = await pw.chromium.launch_persistent_context(
                    str(perfil),
                    locale="es-CL",
                    timezone_id="America/Santiago",
                    viewport={"width": 1280, "height": 800},
                    **opciones,
                )
                try:
                    yield contexto
                finally:
                    await contexto.close()
                return

            navegador = await pw.chromium.launch(**opciones)
            try:
                yield await navegador.new_context(
                    locale="es-CL",
                    timezone_id="America/Santiago",
                )
            finally:
                await navegador.close()


@contextmanager
def _sin_pantalla() -> Iterator[None]:
    """En headless no hace falta pantalla. Existe para no ramificar el `with`."""
    yield


async def iniciar_sesion(
    credenciales: Credenciales,
    *,
    base_url: str = BASE_URL,
    headless: bool = False,
    proxy: str = "",
    args_chromium: tuple[str, ...] = (),
    ejecutable: str = "",
    perfil: Path | None = None,
    captura_fallo: Path | None = None,
    espera_login_s: int = 40,
    timeout_ms: int = 45_000,
) -> Sesion:
    """Entra en prisa.cl con un navegador y devuelve las cookies resultantes.

    Args:
        credenciales: RUT o correo, y contraseña.
        base_url: raíz del sitio. Parametrizada para poder apuntar a un entorno
            de pruebas sin tocar el código.
        headless: **False por defecto**, y no es un descuido. El login no pasa
            en headless —lo mismo que le ocurre al bot de boletas del SII en
            ventu 1.0—, así que se abre con ventana y, en un servidor sin
            monitor, sobre una Xvfb que se levanta sola. Ponerlo en True solo
            tiene sentido con un `perfil` que ya traiga sesión.
        captura_fallo: dónde dejar una foto de la pantalla si el login no pasa.
        espera_login_s: cuánto se sondea tras enviar el formulario antes de dar
            el login por rechazado.
        perfil: directorio de perfil persistente de Chromium. Guarda cookies e
            historial entre corridas, que es lo que hace que el reCAPTCHA deje
            de tratar cada login como un visitante recién llegado.
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
    async with _navegador(
        headless=headless,
        proxy=proxy,
        args_chromium=args_chromium,
        ejecutable=ejecutable,
        perfil=perfil,
    ) as contexto:
        paginas = getattr(contexto, "pages", [])
        pagina = paginas[0] if paginas else await contexto.new_page()
        pagina.set_default_timeout(timeout_ms)

        # `domcontentloaded` y no `networkidle`: la home carga analítica y
        # widgets que no callan nunca, y esperar a que lo hagan es esperar
        # al timeout.
        await pagina.goto(f"{base_url}{RUTA_LOGIN}", wait_until="domcontentloaded")

        # Con perfil persistente puede que ya estemos dentro: el login más barato
        # es el que no se hace, y además no gasta reputación frente al reCAPTCHA.
        if await _esta_autenticado(pagina):
            cookies = {c["name"]: c["value"] for c in await contexto.cookies()}
            log.info("el perfil ya traía sesión de prisa.cl")
            return Sesion(cookies=cookies)

        # El reCAPTCHA invisible necesita haberse inicializado antes del envío.
        # Sin esta espera el POST sale sin el token —que Prisa manda en un campo
        # propio, `google_rechaptcha`, no en el `g-recaptcha-response` estándar.
        await pagina.wait_for_selector(_CAMPO_PASSWORD)
        await pagina.wait_for_timeout(3_000)

        await pagina.fill(_CAMPO_USUARIO, credenciales.usuario)
        await pagina.fill(_CAMPO_PASSWORD, credenciales.password)
        recordarme = await _marcar_recordarme(pagina)
        await pagina.click(_BOTON_ENTRAR)

        entro = await _esperar_autenticado(pagina, segundos=espera_login_s)

        # Las cookies se leen DESPUÉS de confirmar la sesión, no antes: el token
        # persistente de «Recordarme» llega con la navegación posterior al login,
        # y capturarlas demasiado pronto lo deja fuera.
        cookies = {c["name"]: c["value"] for c in await contexto.cookies()}

        if not entro:
            aviso = await _mensaje_del_sitio(pagina)
            foto = await _fotografiar(pagina, captura_fallo)
            log.warning(
                "login rechazado en prisa.cl para %s (quedó en %s)",
                _enmascarar(credenciales.usuario),
                pagina.url,
            )
            raise ErrorLoginPrisa(
                f"prisa.cl no autenticó a {_enmascarar(credenciales.usuario)}"
                + (f": {aviso}" if aviso else f" y no mostró aviso; quedó en {pagina.url}"),
                mensaje_sitio=aviso,
                captura=foto,
            )

        if not _token_persistente(cookies):
            # Sin este token, cada caducidad exige otro navegador. Se avisa en
            # vez de fallar: la sesión sirve igual, solo dura menos. El estado
            # de la casilla va en el aviso porque separa las dos causas: o no se
            # marcó (se arregla aquí) o Prisa no emite el token (no se arregla).
            log.warning(
                "no vino token persistente (casilla marcada: %s): la sesión no se "
                "podrá refrescar sin navegador. Cookies: %s",
                recordarme,
                ", ".join(sorted(cookies)),
            )

        log.info("sesión de prisa.cl iniciada para %s", _enmascarar(credenciales.usuario))
        return Sesion(cookies=cookies)


async def iniciar_sesion_manual(
    *,
    base_url: str = BASE_URL,
    espera_max_s: int = 300,
    proxy: str = "",
    args_chromium: tuple[str, ...] = (),
    ejecutable: str = "",
    headless: bool = False,
    al_abrir: Callable[[], None] | None = None,
) -> Sesion:
    """Abre el navegador, espera a que entres tú, y se queda con las cookies.

    Es la salida cuando el login automático no pasa y no está claro por qué:
    credencial que el proceso recibe mal, una verificación nueva, un cambio en
    el formulario. Lo que produce es exactamente lo mismo —cookies para
    `ClientePrisa`—, así que todo lo de aguas abajo sigue igual.

    También es la respuesta correcta si algún día Prisa añade segundo factor.
    Automatizar un 2FA es pelearse con la medida de seguridad; teclearlo una vez
    cada varias horas, no.

    Args:
        espera_max_s: cuánto se espera a que completes el login antes de
            rendirse. Por defecto cinco minutos.
        headless: solo para tests. Sin ventana no hay nadie que pueda entrar.
        al_abrir: se llama cuando la página ya está lista, para avisar por
            consola. Aquí en vez de un `print` porque este módulo no decide
            cómo se le habla al usuario.

    Raises:
        ErrorLoginPrisa: si se agota la espera sin sesión.
        RuntimeError: si Playwright no está instalado.
    """
    async with _navegador(
        headless=headless, proxy=proxy, args_chromium=args_chromium, ejecutable=ejecutable
    ) as contexto:
        pagina = await contexto.new_page()
        await pagina.goto(f"{base_url}{RUTA_LOGIN}", wait_until="domcontentloaded")

        if al_abrir is not None:
            al_abrir()

        # Se sondea en vez de esperar un selector: el login puede acabar en
        # cualquier página —Oro respeta el `_target_path`— y encadenar esperas
        # a una URL concreta se rompe en cuanto Prisa cambie el destino.
        if await _esperar_autenticado(pagina, segundos=espera_max_s, cada_ms=2_000):
            cookies = {c["name"]: c["value"] for c in await contexto.cookies()}
            log.info("sesión de prisa.cl iniciada a mano")
            return Sesion(cookies=cookies)

        raise ErrorLoginPrisa(
            f"pasaron {espera_max_s}s sin que se completara el login en el navegador"
        )


async def _esperar_autenticado(pagina: Any, *, segundos: int, cada_ms: int = 1_500) -> bool:
    """Sondea hasta que haya sesión, o hasta agotar `segundos`.

    Sondear y no esperar un rato fijo es la diferencia entre detectar el login y
    no detectarlo. Oro envía el formulario por AJAX y redirige después con
    JavaScript, y las páginas de Prisa pesan más de 2 MB: comprobar una sola vez
    tras una espera fija acierta o falla según lo cargada que esté la red. Ese
    fue un fallo real —el login entraba, el navegador se cerraba solo y el
    comando informaba de un rechazo que no había ocurrido—, y el rato fijo hacía
    que pareciera un problema de credenciales.

    Raises:
        ErrorLoginPrisa: si alguien cierra la ventana. Es una cancelación
            legítima, no una avería, y sin esto sale un TargetClosedError de
            Playwright que parece lo segundo.
    """
    for _ in range(max(1, int(segundos * 1_000 / cada_ms))):
        if pagina.is_closed():
            raise ErrorLoginPrisa("se cerró la ventana antes de completar el login")
        if await _esta_autenticado(pagina):
            return True
        await pagina.wait_for_timeout(cada_ms)
    return False


async def _fotografiar(pagina: Any, destino: Path | None) -> Path | None:
    """Guarda una foto de la pantalla. None si no se pidió o si no se pudo.

    Nunca aborta el login por no poder sacarla: es diagnóstico, y perder el
    error real por fallar al fotografiarlo sería el peor cambio posible.
    """
    if destino is None:
        return None
    try:
        destino.parent.mkdir(parents=True, exist_ok=True)
        await pagina.screenshot(path=str(destino), full_page=False)
    except Exception as exc:  # noqa: BLE001
        log.debug("no se pudo capturar la pantalla: %s", exc)
        return None
    return destino


async def _marcar_recordarme(pagina: Any) -> bool:
    """Marca «Recordarme» y devuelve si quedó marcada de verdad.

    En el sitio se llama «No cerrar sesión». Es lo que hace que Symfony emita su
    token persistente, y ese token es la diferencia entre refrescar la sesión sin
    navegador y tener que abrir uno cada vez que caduca.

    La casilla suele estar oculta detrás de un `<label>` estilizado, así que un
    `check()` normal falla por no ser visible. Se fuerza y, si aun así no toma,
    se marca por JS disparando el `change` a mano — sin él, el JS de la página no
    se entera y el formulario viaja sin la casilla.

    Se **relee** el estado en vez de dar por hecho que el clic funcionó: si no
    quedó marcada, quien mira el log necesita saber que el token no llegó por
    esto y no porque Prisa no lo emita. Son dos problemas distintos y solo uno se
    arregla desde aquí.
    """
    try:
        casilla = pagina.locator(_CASILLA_RECORDARME).first
        if await casilla.count() == 0:
            log.warning("no existe la casilla «no cerrar sesión» en el formulario")
            return False
        try:
            await casilla.check(timeout=3_000, force=True)
        except Exception:  # noqa: BLE001 - se reintenta por JS
            await pagina.eval_on_selector(
                _CASILLA_RECORDARME,
                "el => { el.checked = true;"
                " el.dispatchEvent(new Event('change', {bubbles: true})); }",
            )
        marcada = bool(await casilla.is_checked())
        log.info("casilla «no cerrar sesión» marcada: %s", marcada)
        return marcada
    except Exception as exc:  # noqa: BLE001 - no vale la pena abortar el login
        log.debug("no se pudo marcar «recordarme»: %s", exc)
        return False


async def _esta_autenticado(pagina: Any) -> bool:
    """¿La página que tenemos delante es la de alguien con sesión?

    Se busca el enlace de salir en vez de mirar cookies: `OROSFID` existe
    también para un visitante anónimo, así que su presencia no dice nada.

    Si la página está navegando en ese preciso instante, Playwright lanza
    "Execution context was destroyed". Eso no es un fallo: es que todavía no se
    puede mirar, y la respuesta honesta es "no consta", no una excepción. Es
    exactamente lo que pasa al sondear durante el login manual —la navegación
    que se intenta detectar es la que rompe la consulta—, y dejarlo escapar
    tumbaba el comando justo en el momento de acertar.
    """
    try:
        return bool(await pagina.query_selector(f'a[href*="{RUTA_LOGOUT}"]'))
    except Exception as exc:  # noqa: BLE001 - Playwright no tipa este error
        log.debug("la sesión no se pudo comprobar ahora: %s", exc)
        return False


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


# Symfony llama `REMEMBERME` al suyo, pero Oro y sus plantillas lo renombran, así
# que se busca por parecido en vez de por un nombre exacto. Un nombre fijo aquí
# haría que un token que sí llegó se reporte como ausente.
_PISTAS_TOKEN = ("remember", "rememberme", "_security", "persist")


def _token_persistente(cookies: dict[str, str]) -> str | None:
    """Nombre de la cookie que permite refrescar sin navegador, si vino."""
    for nombre in cookies:
        if any(pista in nombre.lower() for pista in _PISTAS_TOKEN):
            return nombre
    return None


def _enmascarar(usuario: str) -> str:
    """Deja el usuario reconocible en un log sin publicarlo entero.

    Un RUT es un identificador personal: la invariante 8 del proyecto prohíbe
    que aparezca completo en los logs.
    """
    if "@" in usuario:
        nombre, _, dominio = usuario.partition("@")
        return f"{nombre[:2]}***@{dominio}"
    return f"{usuario[:3]}***{usuario[-1:]}" if len(usuario) > 4 else "***"
