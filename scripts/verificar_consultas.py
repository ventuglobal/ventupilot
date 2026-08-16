"""Valida contra Postgres que las consultas del catálogo son ejecutables.

Existe por un patrón que ya se repitió tres veces en producción: una consulta
que parece correcta, pasa los tests de texto, y falla en ejecución cuando un
cliente está esperando respuesta.

- `p.id` no existía (la primary key es `clickbox_id`).
- `ORDER BY (precio_final IS NULL)` no resuelve: Postgres solo admite un alias
  del SELECT en el ORDER BY si va suelto, no dentro de una expresión.

Los tests que leen el SQL como texto no pueden ver ninguna de las dos cosas.
Solo el planificador de Postgres puede, y este script se lo pregunta con
`EXPLAIN`: prepara cada consulta con parámetros de mentira y nunca llega a
leer una fila.

Corre en el pre-deploy **sin** `|| true`: si una consulta no es válida, el
despliegue se aborta y el servicio anterior sigue sirviendo. Es la diferencia
entre enterarse en el despliegue o enterarse por un cliente.

Uso:

    python -m scripts.verificar_consultas
"""

from __future__ import annotations

import asyncio
import os
import sys
from decimal import Decimal

import asyncpg

from packages.adapters.catalogo import _ENVOLTURA_BUSQUEDA, _ENVOLTURA_SKUS, CatalogoRepo


# (etiqueta, sql, parámetros) para cada forma que el worker puede ejecutar.
# Los parámetros solo fijan los tipos; con EXPLAIN no se ejecuta nada.
def _casos() -> list[tuple[str, str, tuple[object, ...]]]:
    casos: list[tuple[str, str, tuple[object, ...]]] = []
    for origen, p1, p2 in (
        ("motor", "shopify", 168),
        ("costo", "credito", Decimal("1.4")),
    ):
        # El pool no se usa: solo se le pide la forma del SQL.
        repo = CatalogoRepo(None, origen=origen)
        base, _, _ = repo._base("shopify", 168)  # noqa: SLF001
        casos.append(
            (
                f"buscar[{origen}]",
                _ENVOLTURA_BUSQUEDA[0] + base + _ENVOLTURA_BUSQUEDA[1],
                (p1, p2, "%x%", 9),
            )
        )
        casos.append(
            (
                f"precios_por_sku[{origen}]",
                _ENVOLTURA_SKUS[0] + base + _ENVOLTURA_SKUS[1],
                (p1, p2, ["SKU-X"]),
            )
        )
    return casos


async def verificar(dsn: str) -> int:
    con = await asyncpg.connect(dsn)
    fallos = 0
    try:
        await con.execute("SET default_transaction_read_only = on")
        for etiqueta, sql, args in _casos():
            try:
                await con.execute("EXPLAIN " + sql, *args)
            except Exception as exc:  # noqa: BLE001 - se reportan todas juntas
                fallos += 1
                print(f"  ✗ {etiqueta}: {exc}", file=sys.stderr, flush=True)
            else:
                print(f"  ✓ {etiqueta}", flush=True)
    finally:
        await con.close()
    return fallos


def main() -> int:
    dsn = os.environ.get("DATABASE_URL_RO") or os.environ.get("DATABASE_URL", "")
    if not dsn:
        print("ni DATABASE_URL_RO ni DATABASE_URL están definidas", file=sys.stderr)
        return 1

    print("── validando consultas del catálogo ──", flush=True)
    fallos = asyncio.run(verificar(dsn))
    if fallos:
        print(f"{fallos} consulta(s) inválida(s): se aborta el despliegue", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
