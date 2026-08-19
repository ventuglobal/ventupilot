"""Entra en prisa.cl y guarda la sesión. Es el comando con el que se comprueba
que las credenciales sirven y que el sitio no ha cambiado el formulario.

Existe porque el fallo de este login no se parece a un fallo: sin resolver el
desafío del WAF, o apuntando a `login_check` en vez de `login-check`, el sitio
devuelve 200 con HTML que no es el que se pidió, y el error aparece varias
capas más abajo como "no encuentro el campo". Aquí cada barrera se nombra.

Uso:

    export PRISA_USUARIO="12345678-9"      # RUT con guion, o correo
    export PRISA_PASSWORD="..."
    uv run python -m scripts.prisa_login

    # Ver el navegador mientras lo hace (para depurar cambios del formulario):
    uv run python -m scripts.prisa_login --ver

    # A través de un proxy residencial chileno:
    PRISA_PROXY="http://usuario:clave@proxy.apify.com:8000" \\
        uv run python -m scripts.prisa_login

La contraseña se lee del entorno y nunca de un argumento: `ps` y el historial
del shell son públicos dentro de la máquina.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from packages.adapters.prisa import (
    ClientePrisa,
    Credenciales,
    ErrorLoginPrisa,
    Sesion,
    iniciar_sesion,
)

RUTA_SESION_POR_DEFECTO = ".prisa-sesion.json"


async def entrar(args: argparse.Namespace) -> int:
    try:
        credenciales = Credenciales(
            usuario=os.environ.get("PRISA_USUARIO", ""),
            password=os.environ.get("PRISA_PASSWORD", ""),
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    ruta = Path(os.environ.get("PRISA_SESION_PATH", RUTA_SESION_POR_DEFECTO))

    if not args.forzar:
        guardada = Sesion.cargar(ruta)
        if guardada is not None and not guardada.caducada():
            cliente = ClientePrisa(guardada)
            try:
                if await cliente.esta_viva():
                    print(f"sesión de {ruta} sigue viva ({len(guardada.cookies)} cookies)")
                    print("usa --forzar para renovarla igualmente")
                    return 0
            finally:
                await cliente.cerrar()
            print("la sesión guardada ya no vale; entrando de nuevo")

    # Chromium contra un proxy que retermina TLS puede morir con
    # ERR_CONNECTION_RESET: su ClientHello post-cuántico no cabe en algunos
    # intermediarios. `--ssl-version-max=tls1.2` lo evita, y por eso es una
    # bandera y no un valor fijo: no hace falta en una red normal.
    extra = tuple(a for a in (args.arg or []) if a)

    print("abriendo navegador…", flush=True)
    try:
        sesion = await iniciar_sesion(
            credenciales,
            headless=not args.ver,
            proxy=os.environ.get("PRISA_PROXY", ""),
            args_chromium=extra,
            ejecutable=os.environ.get("PRISA_CHROMIUM_PATH", ""),
        )
    except ErrorLoginPrisa as exc:
        print(f"login rechazado: {exc}", file=sys.stderr)
        if exc.mensaje_sitio:
            print(f"  prisa.cl dijo: {exc.mensaje_sitio}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    sesion.guardar(ruta)
    print(f"dentro. {len(sesion.cookies)} cookies guardadas en {ruta} (0600)")

    cliente = ClientePrisa(sesion)
    try:
        print("comprobando con httpx…", flush=True)
        print("sesión reutilizable:", "sí" if await cliente.esta_viva() else "no")
    finally:
        await cliente.cerrar()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Inicia sesión en prisa.cl")
    parser.add_argument(
        "--ver", action="store_true", help="abre el navegador con ventana"
    )
    parser.add_argument(
        "--forzar", action="store_true", help="entra aunque haya sesión guardada válida"
    )
    parser.add_argument(
        "--arg", action="append", help="bandera extra para Chromium (repetible)"
    )
    return asyncio.run(entrar(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
