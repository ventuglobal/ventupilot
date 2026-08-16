"""Catálogo y precios de ventu 1.0. Solo lectura.

Este módulo usa el pool de `DATABASE_URL_RO`, el rol con `GRANT SELECT` tabla
por tabla. Es la pieza que convierte un prompt injection en un no-evento:
aunque el modelo sea manipulado hasta llamar a esta tool con lo que sea, el
rol con el que se ejecuta no tiene privilegio que escalar.

**La fuente del catálogo es `base_productbase`**, no la tabla de precios. Eso
importa: el motor de precios cubre una parte del catálogo (unos 11.6k de 20.4k
productos con stock, repartidos en canales que se recalculan a ritmos muy
distintos), así que colgar la búsqueda de un `JOIN` interno escondía dos de
cada tres productos y hacía que el agente dijera "no tengo eso" sobre cosas que
sí están en bodega.

El precio se adjunta con `LEFT JOIN`. Un producto sin precio vigente **se
encuentra pero no se cotiza**: `precio_clp` viene en None y
`construir_propuesta` rechaza cualquier SKU sin precio. Esa asimetría es
deliberada — es preferible decir "lo tengo, el precio te lo confirma un
ejecutivo" que callar que existe, y ambas cosas son mejores que inventarse
una cifra.

Por qué no se calcula el precio aquí: `ProductBase.marketplace_price` existe,
pero es una propiedad de Python —costo + fulfillment + markup + IVA + comisión
de MercadoLibre + redondeo— y no una columna. Reimplementarla en SQL sería
duplicar el motor de precios y divergir de él (invariante 1), además de aplicar
una fórmula pensada para publicar en ML a una cotización por WhatsApp.

Qué se excluye del catálogo, y por qué:

- `is_active = false` — el operador lo dio de baja.
- `merged_into_id IS NOT NULL` — es una publicación hija reabsorbida bajo
  otra. Ofrecerla duplicaría el mismo producto con dos SKUs.
- stock nulo o cero — no se ofrece lo que no se puede entregar.
"""

from __future__ import annotations

from decimal import Decimal

import asyncpg

from packages.domain.catalogo import ProductoDisponible, ResultadoBusqueda

# El precio entra por LEFT JOIN, con canal y antigüedad en la condición del
# JOIN y no en el WHERE: puesto en el WHERE, el LEFT JOIN se degrada a INNER y
# volveríamos a esconder los productos sin precio.
#
# OJO con `p.clickbox_id`: `base_productbase` NO tiene columna `id`. Su primary
# key es un UUID llamado `clickbox_id` (ver base.ProductBase en ventu 1.0).
# Escribir `p.id` por costumbre de Django compila en la cabeza pero revienta en
# ejecución, y solo cuando alguien busca algo.
_SELECT_MOTOR = """
    SELECT p.sku,
           p.ventu_sku,
           p.title              AS titulo,
           b.name               AS marca,
           COALESCE(p.stock, 0) AS stock,
           r.precio_final,
           r.channel            AS canal,
           r.calculated_at
      FROM public.base_productbase p
      LEFT JOIN public.base_brand b
        ON b.id = p.brand_id
      LEFT JOIN public.pricing_productpriceresult r
        ON r.product_id = p.clickbox_id
       AND r.channel = $1
       AND r.precio_final > 0
       AND r.calculated_at >= now() - make_interval(hours => $2)
     WHERE p.is_active
       AND p.merged_into_id IS NULL
       AND COALESCE(p.stock, 0) > 0
"""

# Precio derivado del costo, sin pasar por el motor.
#
# `$1` es el nombre de la columna de costo y `$2` el factor. El nombre no se
# interpola: se elige con un CASE sobre un valor que la config ya restringió a
# dos literales, para que ninguna ruta pueda acabar concatenando texto en el
# SQL.
#
# `round(...)::bigint` produce pesos enteros. El peso chileno no tiene
# subdivisión en circulación y el resto del sistema trabaja en enteros; dejar
# decimales aquí los arrastraría hasta el mensaje del cliente.
#
# Un producto sin ese costo queda con precio NULL: se encuentra pero no se
# cotiza. No se cae al otro costo a propósito — `costo_contado` es NETO y
# `costo_credito` bruto, así que aplicarles el mismo factor daría precios de
# bases distintas mezclados en la misma cotización.
_SELECT_COSTO = """
    SELECT p.sku,
           p.ventu_sku,
           p.title              AS titulo,
           b.name               AS marca,
           COALESCE(p.stock, 0) AS stock,
           round(
             CASE WHEN $1 = 'contado' THEN p.costo_contado ELSE p.costo_credito END
             * $2::numeric
           )::bigint            AS precio_final,
           $1::text             AS canal,
           now()                AS calculated_at
      FROM public.base_productbase p
      LEFT JOIN public.base_brand b
        ON b.id = p.brand_id
     WHERE p.is_active
       AND p.merged_into_id IS NULL
       AND COALESCE(p.stock, 0) > 0
"""


# Envuelve cualquiera de las dos consultas base para filtrar por SKU y exigir
# precio. Constantes para que la construcción del SQL no concatene nada que
# venga de fuera.
_ENVOLTURA_SKUS = (
    "SELECT * FROM (",
    "       AND p.sku = ANY($3::text[])) q WHERE q.precio_final IS NOT NULL",
)


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
    """Lectura del catálogo. Recibe el pool de solo lectura.

    `origen` decide de dónde sale el precio: del motor de ventu 1.0 o del
    costo por un factor. Los dos primeros parámetros de la consulta cambian de
    significado según cuál sea, y por eso se resuelven juntos en `_base`.
    """

    def __init__(
        self,
        pool_ro: asyncpg.Pool,
        *,
        origen: str = "motor",
        factor: Decimal = Decimal("1.4"),
        costo: str = "credito",
    ) -> None:
        self._pool = pool_ro
        self._origen = origen
        self._factor = factor
        self._costo = costo

    def _base(self, canal: str, max_edad_horas: int) -> tuple[str, object, object]:
        """Devuelve (sql, $1, $2) según el origen del precio."""
        if self._origen == "costo":
            return _SELECT_COSTO, self._costo, self._factor
        return _SELECT_MOTOR, canal, max_edad_horas

    async def buscar(
        self, texto: str, *, canal: str, max_edad_horas: int, limite: int
    ) -> ResultadoBusqueda:
        """Busca productos por texto libre en `base_productbase`.

        Es un ILIKE sobre título, SKU, SKU Ventu, modelo y part number. No es
        búsqueda semántica y no pretende serlo: en catálogo industrial el
        cliente suele escribir el modelo o parte del nombre exacto, y esto lo
        cubre con latencia predecible. Si más adelante hace falta relevancia
        real, este es el único sitio que cambia.

        Los que tienen precio vigente van primero: son los que se pueden
        cotizar, y el modelo tiene un tope de resultados. Sin ese orden, un
        recorte podría dejar fuera justo los ofrecibles.

        Se pide `limite + 1` para saber si hay más resultados sin pagar un
        COUNT(*) sobre el catálogo entero.
        """
        patron = f"%{texto.strip()}%"
        base, p1, p2 = self._base(canal, max_edad_horas)
        sql = (
            base
            + """
       AND (p.title ILIKE $3
            OR p.sku ILIKE $3
            OR p.ventu_sku ILIKE $3
            OR p.model ILIKE $3
            OR p.part_number ILIKE $3)
     ORDER BY (precio_final IS NULL), p.stock DESC, p.title
     LIMIT $4
    """
        )
        async with self._pool.acquire() as conn:
            filas = await conn.fetch(sql, p1, p2, patron, limite + 1)

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

        Solo devuelve los que tienen precio vigente: es la puerta por la que
        `construir_propuesta` rechaza cotizar lo que no se puede cotizar.

        Se vuelve a consultar al construir la propuesta en vez de confiar en
        lo que el modelo vio durante la búsqueda. Entre una cosa y otra pueden
        pasar varios turnos y el motor de precios corre de forma continua:
        cotizar con el precio que el modelo recuerda sería cotizar un precio
        que ya no existe.
        """
        if not skus:
            return {}

        base, p1, p2 = self._base(canal, max_edad_horas)
        # Subconsulta porque con `precio_origen="costo"` el precio es una
        # expresión calculada, y una expresión del SELECT no se puede
        # referenciar desde el WHERE del mismo nivel.
        #
        # El noqa es seguro y no un atajo: `base` es una de las dos constantes
        # de este módulo y el resto son literales. Todo lo que viene de fuera
        # —costo, factor, SKUs— viaja como parámetro enlazado ($1..$3).
        sql = _ENVOLTURA_SKUS[0] + base + _ENVOLTURA_SKUS[1]  # noqa: S608
        async with self._pool.acquire() as conn:
            filas = await conn.fetch(sql, p1, p2, skus)

        return {f["sku"]: (f["titulo"], f["precio_final"]) for f in filas}
