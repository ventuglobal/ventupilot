"""Aplica las migraciones SQL en orden. Idempotente.

Existe porque la Postgres de ventu-prod no es alcanzable desde fuera de la red
privada de Railway, así que no siempre se puede hacer el `psql -f` a mano que
describe DEPLOY.md. Puesto como `preDeployCommand` del worker, las migraciones
se aplican dentro de la red y antes de que la nueva versión reciba tráfico.

Uso:

    python -m scripts.migrate

Todo el DDL de `migrations/` usa `IF NOT EXISTS`, así que reejecutarlo en cada
despliegue no hace daño. Eso es deliberado: una migración que solo se puede
correr una vez es una que alguien acabará corriendo dos.

Las migraciones se aplican **cada una en su propia transacción**, por orden de
nombre de archivo. Si una falla, las anteriores quedan aplicadas y el proceso
sale con código 1 — el despliegue se aborta y el servicio viejo sigue sirviendo.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import asyncpg

DIRECTORIO = Path(__file__).resolve().parent.parent / "migrations"


async def aplicar(dsn: str) -> int:
    archivos = sorted(DIRECTORIO.glob("*.sql"))
    if not archivos:
        print(f"no hay migraciones en {DIRECTORIO}", file=sys.stderr)
        return 1

    con = await asyncpg.connect(dsn)
    try:
        for archivo in archivos:
            # Cada archivo trae su propio BEGIN/COMMIT donde hace falta; se
            # ejecuta entero con el protocolo simple para que los bloques
            # `DO $$ ... $$` pasen sin que asyncpg intente prepararlos.
            await con.execute(archivo.read_text(encoding="utf-8"))
            print(f"migración aplicada: {archivo.name}", flush=True)
    finally:
        await con.close()
    return 0


def main() -> int:
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        print("DATABASE_URL no está definida", file=sys.stderr)
        return 1
    return asyncio.run(aplicar(dsn))


if __name__ == "__main__":
    raise SystemExit(main())
