"""Catálogo y precios de ventu 1.0. Solo lectura.

Este módulo usa el pool de `DATABASE_URL_RO`, el rol con `GRANT SELECT` tabla
por tabla. Es la pieza que convierte un prompt injection en un no-evento:
aunque el modelo sea manipulado hasta llamar a esta tool con lo que sea, el
rol con el que se ejecuta no tiene privilegio que escalar.

De dónde sale el precio: de `pricing_productpriceresult.precio_final`, la
salida del motor de precios de ventu 1.0 para un producto y un canal. El
motor ya resolvió costo, markup, IVA, redondeo y campañas. Recalcularlo
aquí sería reimplementarlo y divergir (invariante 1).

Qué se excluye del catálogo ofrecible, y por qué:

- `is_active = false` — el operador lo dio de baja.
- `merged_into_id IS NOT NULL` — es una publicación hija reabsorbida bajo
  otra. Ofrecerla duplicaría el mismo producto con dos SKUs.
- stock nulo o cero — no se cotiza lo que no se puede entregar.
- `precio_final <= 0` — el motor no llegó a un precio válido.
- cálculo rancio — un precio de hace semanas no es un precio.
"""

from __future__ import annotations

import asyncpg

from packages.domain.catalogo import ProductoDisponible, ResultadoBusqueda

# El join a marca es LEFT porque `brand` es nullable en ventu 1.0 y un
# producto sin marca sigue siendo vendible.
_SELECT = """
    SELECT p.sku,
           p.ventu_sku,
           p.title              AS titulo,
           b.name               AS marca,
           COALESCE(p.stock, 0) AS stock,
           r.precio_final,
           r.channel            AS canal,
           r.calculated_at
      FROM public.base_productbase p
      JOIN public.pricing_productpriceresult r
        -- OJO: `base_productbase` NO tiene columna `id`. Su primary key es
        -- `clickbox_id`, un UUID (ver base.ProductBase en ventu 1.0). Escribir
        -- `p.id` por costumbre de Django compila en la cabeza pero revienta en
        -- ejecución, y solo cuando alguien busca algo.
        ON r.product_id = p.clickbox_id
       AND r.channel = $1
      LEFT JOIN public.base_brand b
        ON b.id = p.brand_id
     WHERE p.is_active
       AND p.merged_into_id IS NULL
       AND COALESCE(p.stock, 0) > 0
       AND r.precio_final > 0
       AND r.calculated_at >= now() - make_interval(hours => $2)
"""


def _a_producto(fila: asyncpg.Record) -> ProductoDisponible:
    return ProductoDisponible(
        sku=fila["sku"],
        ventu_sku=fila["ventu_sku"],
        titulo=fila["titulo"],
        marca=fila["marca"],
        stock=fila["stock"],
        precio_clp=fila["precio_final"],
        canal=fila["canal"],
        precio_calculado_at=fila["calculated_at"],
    )


class CatalogoRepo:
    """Lectura del catálogo. Recibe el pool de solo lectura."""

    def __init__(self, pool_ro: asyncpg.Pool) -> None:
        self._pool = pool_ro

    async def buscar(
        self, texto: str, *, canal: str, max_edad_horas: int, limite: int
    ) -> ResultadoBusqueda:
        """Busca productos ofrecibles por texto libre.

        Es un ILIKE sobre título, SKU, SKU Ventu, modelo y part number. No es
        búsqueda semántica y no pretende serlo: en catálogo industrial el
        cliente suele escribir el modelo o parte del nombre exacto, y esto lo
        cubre con latencia predecible. Si más adelante hace falta relevancia
        real, este es el único sitio que cambia.

        Se pide `limite + 1` para saber si hay más resultados sin pagar un
        COUNT(*) sobre el catálogo entero. Que el modelo sepa que está viendo
        un recorte cambia cómo redacta la respuesta.
        """
        patron = f"%{texto.strip()}%"
        sql = (
            _SELECT
            + """
       AND (p.title ILIKE $3
            OR p.sku ILIKE $3
            OR p.ventu_sku ILIKE $3
            OR p.model ILIKE $3
            OR p.part_number ILIKE $3)
     ORDER BY p.stock DESC, p.title
     LIMIT $4
    """
        )
        async with self._pool.acquire() as conn:
            filas = await conn.fetch(sql, canal, max_edad_horas, patron, limite + 1)

        truncado = len(filas) > limite
        productos = [_a_producto(f) for f in filas[:limite]]
        return ResultadoBusqueda(
            productos=productos,
            total_encontrados=len(productos),
            truncado=truncado,
        )

    async def precios_por_sku(
        self, skus: list[str], *, canal: str, max_edad_horas: int
    ) -> dict[str, tuple[str, int]]:
        """Resuelve `sku → (titulo, precio_unitario_clp)` para valorizar.

        Se vuelve a consultar al construir la propuesta en vez de confiar en
        lo que el modelo vio durante la búsqueda. Entre una cosa y otra pueden
        pasar varios turnos y el motor de precios corre de forma continua:
        cotizar con el precio que el modelo recuerda sería cotizar un precio
        que ya no existe.

        Un SKU ausente del resultado es un SKU sin precio vigente. El llamador
        decide; `construir_propuesta` lanza.
        """
        if not skus:
            return {}

        sql = _SELECT + "       AND p.sku = ANY($3::text[])"
        async with self._pool.acquire() as conn:
            filas = await conn.fetch(sql, canal, max_edad_horas, skus)

        return {f["sku"]: (f["titulo"], f["precio_final"]) for f in filas}
