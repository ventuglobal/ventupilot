"""Crea el índice de trigramas que hace usable la búsqueda del catálogo.

Sin él, `ILIKE '%texto%'` sobre `base_productbase` recorre las ~28k filas en
cada consulta: el comodín inicial impide usar cualquier índice B-tree, y el
cliente espera segundos por cada búsqueda del agente.

Vive fuera de `migrations/` por dos motivos:

1. `CREATE INDEX CONCURRENTLY` **no puede correr dentro de una transacción**, y
   los archivos de `migrations/` van envueltos en BEGIN/COMMIT. Aquí cada
   sentencia se manda por separado, en autocommit.
2. Es un índice sobre una tabla de **ventu 1.0**, no sobre el esquema
   `ventupilot`. Tenerlo aparte deja claro que esto toca territorio ajeno y
   que se aplicó a propósito.

`CONCURRENTLY` no bloquea escrituras: ventu 1.0 sigue operando mientras se
construye. En una tabla de este tamaño tarda segundos.

Idempotente. Uso:

    python -m scripts.indices
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

import asyncpg

from packages.adapters.busqueda import EXPRESION_INDICE, NOMBRE_INDICE

# Cada sentencia se ejecuta por separado: mandarlas juntas las metería en una
# transacción implícita y CONCURRENTLY fallaría.
SENTENCIAS = [
    ("extensión pg_trgm", "CREATE EXTENSION IF NOT EXISTS pg_trgm"),
    (
        f"índice {NOMBRE_INDICE}",
        f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {NOMBRE_INDICE} "
        f"ON public.base_productbase USING gin ({EXPRESION_INDICE} gin_trgm_ops)",
    ),
]


async def aplicar(dsn: str) -> int:
    con = await asyncpg.connect(dsn)
    fallos = 0
    try:
        for etiqueta, sql in SENTENCIAS:
            inicio = time.monotonic()
            try:
                await con.execute(sql)
            except asyncpg.InsufficientPrivilegeError as exc:
                # Crear la extensión suele necesitar superusuario. No es fatal:
                # si ya está instalada, el índice se crea igual.
                fallos += 1
                print(f"  ✗ {etiqueta}: sin privilegios ({exc})", file=sys.stderr, flush=True)
            except Exception as exc:  # noqa: BLE001 - se reportan todos
                fallos += 1
                print(f"  ✗ {etiqueta}: {exc}", file=sys.stderr, flush=True)
            else:
                print(f"  ✓ {etiqueta} ({time.monotonic() - inicio:.1f}s)", flush=True)

        # Un CONCURRENTLY interrumpido deja el índice marcado como inválido y
        # el planificador lo ignora en silencio: la búsqueda vuelve a ser lenta
        # sin que nada falle. Conviene verlo en el log del despliegue.
        invalido = await con.fetchval(
            "select not indisvalid from pg_index i"
            " join pg_class c on c.oid = i.indexrelid"
            " where c.relname = $1",
            NOMBRE_INDICE,
        )
        if invalido:
            print(
                f"  ! {NOMBRE_INDICE} existe pero está INVÁLIDO:"
                " un CONCURRENTLY quedó a medias. Hay que borrarlo y rehacerlo.",
                file=sys.stderr,
                flush=True,
            )
    finally:
        await con.close()
    return fallos


def main() -> int:
    # Solo DATABASE_URL: el rol de lectura no puede crear índices.
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        print("DATABASE_URL no está definida", file=sys.stderr)
        return 1

    print("── índices de búsqueda ──", flush=True)
    return 1 if asyncio.run(aplicar(dsn)) else 0


if __name__ == "__main__":
    raise SystemExit(main())
