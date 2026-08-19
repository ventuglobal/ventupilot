"""Trae una página de prisa.cl con la sesión guardada y cuenta qué llegó.

Existe para responder dos preguntas que un `status_code` no responde:

- **¿Es la página que pedí?** Un `200` de 1,8 MB puede ser perfectamente el
  formulario de login: OroCommerce redirige a los anónimos en vez de dar 401 o
  403, y `httpx` sigue el redirect sin rechistar. Mirar el código de estado es
  precisamente lo que hace creer que la sesión funciona cuando no.
- **¿Qué hay dentro que valga la pena?** Antes de escribir un parser conviene
  saber cuántas tablas hay y qué pinta tienen.

Uso:

    uv run python -m scripts.prisa_ver /customer/order/
    uv run python -m scripts.prisa_ver /customer/order/ --estructura
    uv run python -m scripts.prisa_ver /customer/order/ --guardar pedidos.html

Entrecomilla la ruta si lleva `?`: zsh la trata como patron de fichero y
responde "no matches found" antes de que este script llegue a ejecutarse.

    uv run python -m scripts.prisa_ver '/product/search?search=resma'

Guardar el HTML permite iterar sobre el parser sin volver a pedirle nada al
sitio, que es más rápido y más educado con Prisa.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

from packages.adapters.config import get_prisa_settings
from packages.adapters.prisa import ClientePrisa, Sesion
from packages.adapters.prisa.sesion import RUTA_LOGIN, RUTA_LOGOUT

_RE_TITULO = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)
# OroCommerce pinta sus listados con datagrids de JavaScript, no con <table>.
# Estos son los rastros que deja en el HTML y que dicen dónde está el JSON.
_RE_GRID_NOMBRE = re.compile(r'["\']gridName["\']\s*:\s*["\']([\w-]+)["\']')
_RE_GRID_URL = re.compile(r'(/datagrid/[\w/-]+|/api/rest/[\w/-]+|/ajax/[\w/-]+)')
_RE_COMPONENTE = re.compile(r'data-page-component-module=["\']([^"\']+)["\']')
_RE_TABLA = re.compile(r"<table", re.I)
_RE_ETIQUETAS = re.compile(r"<[^>]+>")
# Se quitan enteros, contenido incluido. En una página de Oro el JavaScript es
# el 95% de los bytes —analítica, New Relic, el bundle de la tienda—, así que
# sin esto "el texto" son mil líneas de bundle y ni rastro de lo que se busca.
_RE_NO_TEXTO = re.compile(r"<(script|style|noscript|template)\b.*?</\1>", re.S | re.I)
_RE_ESPACIOS = re.compile(r"\s+")


async def ver(args: argparse.Namespace) -> int:
    cfg = get_prisa_settings()
    ruta_sesion = Path(cfg.prisa_sesion_path)

    sesion = Sesion.cargar(ruta_sesion)
    if sesion is None:
        print(f"no hay sesión en {ruta_sesion}", file=sys.stderr)
        print("entra primero:  uv run python -m scripts.prisa_login", file=sys.stderr)
        return 2

    cliente = ClientePrisa(sesion, base_url=cfg.prisa_base_url)
    try:
        respuesta = await cliente.obtener(args.ruta)
    finally:
        await cliente.cerrar()

    html = respuesta.text
    titulo = _RE_TITULO.search(html)
    # El enlace de salir es la única señal fiable: la cookie de sesión existe
    # también para un visitante anónimo.
    autenticado = RUTA_LOGOUT in html
    parece_login = RUTA_LOGIN in str(respuesta.url) or "_csrf_token" in html

    print(f"pedido      {args.ruta}")
    print(f"llegó a     {respuesta.url}")
    print(f"estado      {respuesta.status_code}")
    print(f"tamaño      {len(html):,} bytes".replace(",", "."))
    print(f"título      {_limpiar(titulo.group(1)) if titulo else '(sin título)'}")
    print(f"con sesión  {'sí' if autenticado else 'NO — te devolvió contenido anónimo'}")
    print(f"tablas      {len(_RE_TABLA.findall(html))}")

    if not autenticado and parece_login:
        print()
        print("esto es el formulario de login, no lo que pediste.", file=sys.stderr)
        print("la sesión caducó; vuelve a entrar con scripts.prisa_login", file=sys.stderr)

    if args.guardar:
        destino = Path(args.guardar)
        destino.write_text(html, encoding="utf-8")
        print(f"guardado    {destino}")

    if args.estructura:
        _estructura(html)

    if args.texto:
        print("\n--- texto ---")
        print(_texto_visible(html)[: args.texto])

    return 0 if autenticado else 1


def _estructura(html: str) -> None:
    """Dice de dónde saldrían los datos, sin imprimir ninguno.

    Un listado de OroCommerce no está en el HTML: lo pinta un datagrid de
    JavaScript que pide el contenido aparte. Por eso `tablas 0` en una página
    que a ojo tiene una tabla — y por eso no sirve de nada buscar `<tr>`.

    Lo que interesa es el nombre de la rejilla y la URL que consulta: con eso
    se le puede pedir el JSON directamente a `httpx`, que es mucho más barato
    que levantar un navegador para leer el DOM ya pintado.

    Solo se imprimen nombres y rutas, nunca valores: esta salida se pega en un
    chat o en un issue, y los datos de la cuenta no tienen por qué viajar ahí.
    """
    print("\n--- de dónde saldrían los datos ---")

    rejillas = sorted(set(_RE_GRID_NOMBRE.findall(html)))
    print(f"rejillas    {', '.join(rejillas) if rejillas else '(ninguna)'}")

    urls = sorted({u for u in _RE_GRID_URL.findall(html) if len(u) < 120})
    print(f"endpoints   {len(urls)}")
    for url in urls[:12]:
        print(f"            {url}")
    if len(urls) > 12:
        print(f"            … y {len(urls) - 12} más")

    componentes = sorted(set(_RE_COMPONENTE.findall(html)))
    print(f"componentes {len(componentes)}")
    for comp in componentes[:8]:
        print(f"            {comp}")

    print(f"filas <tr>  {len(re.findall(r'<tr', html, re.I))}")


def _texto_visible(html: str) -> str:
    """El texto que leería una persona, sin el JavaScript de por medio."""
    return _limpiar(_RE_ETIQUETAS.sub(" ", _RE_NO_TEXTO.sub(" ", html)))


def _limpiar(texto: str) -> str:
    return _RE_ESPACIOS.sub(" ", texto).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Trae una página de prisa.cl")
    parser.add_argument("ruta", nargs="?", default="/customer/order/")
    parser.add_argument("--guardar", help="vuelca el HTML a un fichero")
    parser.add_argument(
        "--estructura",
        action="store_true",
        help="dice de dónde saldrían los datos (nombres y rutas, ningún valor)",
    )
    parser.add_argument(
        "--texto",
        nargs="?",
        type=int,
        const=1500,
        default=0,
        help="imprime los primeros N caracteres de texto plano (1500 por defecto)",
    )
    return asyncio.run(ver(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
