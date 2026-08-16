"""Cuenta qué parte del catálogo de ventu 1.0 es ofrecible, y por qué no el resto.

Existe porque el agente respondía "no encuentro nada" y no había forma de saber
si el problema era la consulta, el canal, la antigüedad del precio o
sencillamente que no hay productos publicables. Adivinarlo cuesta un
despliegue por hipótesis; medirlo cuesta uno.

Solo lectura: abre la transacción en `read only` para que no pueda escribir
nada en la base de producción aunque alguien añada una consulta descuidada.

Uso:

    python -m scripts.diagnostico
"""

from __future__ import annotations

import asyncio
import os
import sys

import asyncpg

# Cada paso descuenta del anterior, así que la primera línea que caiga a cero
# señala la causa. Es el orden de los filtros de `adapters/catalogo.py`.
EMBUDO = [
    ("productos", "select count(*) from public.base_productbase"),
    ("activos", "select count(*) from public.base_productbase where is_active"),
    (
        "no reabsorbidos",
        "select count(*) from public.base_productbase where is_active and merged_into_id is null",
    ),
    (
        "con stock",
        "select count(*) from public.base_productbase where is_active"
        " and merged_into_id is null and coalesce(stock,0) > 0",
    ),
    (
        "con fila de precio (cualquier canal)",
        "select count(*) from public.base_productbase p"
        " join public.pricing_productpriceresult r on r.product_id = p.clickbox_id"
        " where p.is_active and p.merged_into_id is null and coalesce(p.stock,0) > 0",
    ),
    (
        "con precio > 0",
        "select count(*) from public.base_productbase p"
        " join public.pricing_productpriceresult r on r.product_id = p.clickbox_id"
        " where p.is_active and p.merged_into_id is null and coalesce(p.stock,0) > 0"
        " and r.precio_final > 0",
    ),
]


async def diagnosticar(dsn: str) -> None:
    con = await asyncpg.connect(dsn)
    try:
        await con.execute("SET default_transaction_read_only = on")

        print("── embudo de ofrecibles ──", flush=True)
        for etiqueta, sql in EMBUDO:
            print(f"  {etiqueta:38} {await con.fetchval(sql)}", flush=True)

        print("── filas de precio por canal ──", flush=True)
        for fila in await con.fetch(
            "select channel, count(*) n, max(calculated_at) ultimo"
            " from public.pricing_productpriceresult group by channel order by n desc"
        ):
            print(
                f"  {fila['channel']:10} filas={fila['n']:<8} último={fila['ultimo']}",
                flush=True,
            )

        print("── etapa de ciclo de vida (activos) ──", flush=True)
        for fila in await con.fetch(
            "select coalesce(lifecycle_stage,'(sin etapa)') etapa, count(*) n"
            " from public.base_productbase where is_active group by 1 order by n desc"
        ):
            print(f"  {fila['etapa']:16} {fila['n']}", flush=True)
    finally:
        await con.close()


def main() -> int:
    dsn = os.environ.get("DATABASE_URL_RO") or os.environ.get("DATABASE_URL", "")
    if not dsn:
        print("ni DATABASE_URL_RO ni DATABASE_URL están definidas", file=sys.stderr)
        return 1
    asyncio.run(diagnosticar(dsn))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
