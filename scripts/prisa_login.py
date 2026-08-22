"""Entra en prisa.cl y guarda la sesión. Es el comando con el que se comprueba
que las credenciales sirven y que el sitio no ha cambiado el formulario.

Existe porque el fallo de este login no se parece a un fallo: sin resolver el
desafío del WAF, o apuntando a `login_check` en vez de `login-check`, el sitio
devuelve 200 con HTML que no es el que se pidió, y el error aparece varias
capas más abajo como "no encuentro el campo". Aquí cada barrera se nombra.

Uso: pon las credenciales en `.env` y corre

    uv run python -m scripts.prisa_login

    # Sin ventana. Solo sirve si el perfil persistente ya trae sesión:
    # el login desde cero no pasa en headless.
    uv run python -m scripts.prisa_login --headless

`.env` antes que `export`, y no es solo por seguir la convención del proyecto:
`export PRISA_PASSWORD="clave$con!signos"` deja que el shell se coma el `$` y
el `!`, y el sitio responde «los datos ingresados son incorrectos», que apunta
al sitio equivocado. En el fichero no hay expansión. Si prefieres exportar, usa
comillas simples.

Nunca por argumento: `ps` y el historial del shell son públicos en la máquina.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from packages.adapters.config import get_prisa_settings
from packages.adapters.prisa import (
    ClientePrisa,
    Credenciales,
    ErrorLoginPrisa,
    Sesion,
    iniciar_sesion,
    iniciar_sesion_manual,
)


async def entrar(args: argparse.Namespace) -> int:
    cfg = get_prisa_settings()
    try:
        credenciales = (
            None
            if args.manual
            else Credenciales(usuario=cfg.prisa_usuario, password=cfg.prisa_password)
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(
            "defínelos en .env (recomendado: el shell no expande nada ahí)"
            " o expórtalos con comillas simples."
            "  Sin credenciales, --manual te deja entrar a mano.",
            file=sys.stderr,
        )
        return 2

    ruta = Path(cfg.prisa_sesion_path)

    if not args.forzar:
        guardada = Sesion.cargar(ruta)
        if guardada is not None and not guardada.caducada():
            cliente = ClientePrisa(guardada, base_url=cfg.prisa_base_url)
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
        if credenciales is None:
            sesion = await iniciar_sesion_manual(
                base_url=cfg.prisa_base_url,
                espera_max_s=args.espera,
                proxy=cfg.prisa_proxy,
                args_chromium=extra,
                ejecutable=cfg.prisa_chromium_path,
                al_abrir=lambda: print(
                    f"entra tú en la ventana que se abrió. Tienes {args.espera}s;"
                    " en cuanto estés dentro, me quedo con la sesión.",
                    flush=True,
                ),
            )
        else:
            sesion = await iniciar_sesion(
                credenciales,
                headless=args.headless,
                base_url=cfg.prisa_base_url,
                perfil=Path(cfg.prisa_perfil_path) if cfg.prisa_perfil_path else None,
                captura_fallo=Path("prisa-fallo.png"),
                proxy=cfg.prisa_proxy,
                args_chromium=extra,
                ejecutable=cfg.prisa_chromium_path,
            )
    except ErrorLoginPrisa as exc:
        print(f"login rechazado: {exc}", file=sys.stderr)
        if exc.mensaje_sitio:
            print(f"  prisa.cl dijo: {exc.mensaje_sitio}", file=sys.stderr)
        if exc.captura is not None:
            print(f"  foto de la pantalla: {exc.captura}", file=sys.stderr)
        # Lo que de verdad hay que descartar antes de dudar de la cuenta es que
        # el shell haya mordido la contraseña. La longitud lo delata sin
        # imprimirla, y así no hace falta que nadie la escriba en otro sitio
        # para comprobarlo.
        if credenciales is not None:
            print(
                f"  se envió usuario {cfg.prisa_usuario!r} con una contraseña de "
                f"{len(cfg.prisa_password)} caracteres — si ese número no es el que "
                "esperas, el shell se comió parte: usa .env o comillas simples",
                file=sys.stderr,
            )
            print(
                "  si el número cuadra y prisa.cl la sigue rechazando, entra a mano:"
                "  uv run python -m scripts.prisa_login --manual",
                file=sys.stderr,
            )
        return 1
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    sesion.guardar(ruta)
    print(f"dentro. {len(sesion.cookies)} cookies guardadas en {ruta} (0600)")
    # Los NOMBRES, nunca los valores: con los nombres se ve si vino el token
    # persistente, y esta salida se acaba pegando en un chat.
    print(f"  cookies: {', '.join(sorted(sesion.cookies))}")

    cliente = ClientePrisa(sesion, base_url=cfg.prisa_base_url)
    try:
        print("comprobando con httpx…", flush=True)
        print("sesión reutilizable:", "sí" if await cliente.esta_viva() else "no")
    finally:
        await cliente.cerrar()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Inicia sesión en prisa.cl")
    parser.add_argument(
        "--headless",
        action="store_true",
        help="sin ventana. Solo funciona si el perfil ya trae sesión: el login "
        "de prisa.cl no pasa en headless desde cero",
    )
    parser.add_argument(
        "--forzar", action="store_true", help="entra aunque haya sesión guardada válida"
    )
    parser.add_argument(
        "--arg", action="append", help="bandera extra para Chromium (repetible)"
    )
    parser.add_argument(
        "--manual",
        action="store_true",
        help="abre el navegador para que entres tú y se queda con la sesión",
    )
    parser.add_argument(
        "--espera", type=int, default=300, help="segundos de espera con --manual"
    )
    return asyncio.run(entrar(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
