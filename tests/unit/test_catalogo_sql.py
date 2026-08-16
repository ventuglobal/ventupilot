"""Fija las columnas de ventu 1.0 que la consulta de catálogo asume.

Estos tests no tocan la base: leen el SQL. Suena pobre, pero cubren un fallo
que ya ocurrió en producción y que ningún test de lógica podía ver.

`base_productbase` **no tiene columna `id`**: su primary key es `clickbox_id`,
un UUID. Escribir `p.id` es el reflejo natural de cualquiera acostumbrado a
Django, compila perfectamente, y solo falla en ejecución — cuando un cliente
ya está esperando una respuesta.

Mientras no haya un test de integración contra una Postgres real, esto es lo
que impide que el reflejo vuelva.
"""

from __future__ import annotations

import re

from packages.adapters.catalogo import _SELECT

# Los comentarios `--` se quitan porque el propio SQL advierte sobre `p.id`;
# comprobar el texto crudo daría un falso positivo contra la advertencia.
_SQL = "\n".join(línea.split("--")[0] for línea in _SELECT.splitlines())


def test_el_join_usa_clickbox_id_y_no_id():
    """La primary key de base_productbase es clickbox_id, no id."""
    assert "r.product_id = p.clickbox_id" in _SQL
    # `p.id` a secas no debe aparecer en el SQL efectivo.
    assert not re.search(r"\bp\.id\b", _SQL)


def test_las_tablas_referenciadas_son_las_esperadas():
    """Si ventu 1.0 renombra una tabla, que salte aquí y no en producción."""
    for tabla in (
        "public.base_productbase",
        "public.pricing_productpriceresult",
        "public.base_brand",
    ):
        assert tabla in _SQL


def test_se_filtran_los_no_ofrecibles():
    """Las exclusiones son decisiones de producto, no detalles de la consulta.

    Un producto reabsorbido bajo otra publicación se ofrecería dos veces; uno
    sin stock no se puede entregar; un precio no calculado no es un precio.
    """
    assert "p.is_active" in _SQL
    assert "p.merged_into_id IS NULL" in _SQL
    assert "COALESCE(p.stock, 0) > 0" in _SQL
    assert "r.precio_final > 0" in _SQL


def test_hay_tope_de_antiguedad_del_precio():
    """Un precio recalculado hace semanas no es un precio (invariante 1)."""
    assert "r.calculated_at >= now() - make_interval(hours =>" in _SQL


def test_no_se_expone_costo_ni_margen_al_modelo():
    """El agente no negocia márgenes; esos datos no deben poder filtrarse."""
    for prohibido in ("r.costo", "r.margen_final", "p.costo_credito", "p.costo_contado"):
        assert prohibido not in _SQL


# ── La búsqueda parte de base_productbase, no de los precios ─────────────────


def test_el_precio_entra_por_left_join():
    """El catálogo es base_productbase; el precio se adjunta si existe.

    Con un JOIN interno, los ~9k productos con stock y sin precio calculado
    desaparecían y el agente decía "no tengo eso" sobre cosas en bodega.
    """
    assert "LEFT JOIN public.pricing_productpriceresult" in _SQL


def test_las_condiciones_de_precio_van_en_el_join_no_en_el_where():
    """Puestas en el WHERE, el LEFT JOIN se degrada a INNER en silencio.

    Es el error clásico y no da ningún síntoma: la consulta sigue siendo
    válida y simplemente vuelve a esconder los productos sin precio.
    """
    join, _, where = _SQL.partition("WHERE")
    for condicion in ("r.channel =", "r.precio_final > 0", "r.calculated_at >="):
        assert condicion in join, f"{condicion} debe estar en el JOIN"
        assert condicion not in where, f"{condicion} en el WHERE degrada el LEFT JOIN"


def test_el_where_solo_filtra_el_producto():
    """Lo que excluye productos es del producto, no de su precio."""
    _, _, where = _SQL.partition("WHERE")
    assert "p.is_active" in where
    assert "p.merged_into_id IS NULL" in where
    assert "COALESCE(p.stock, 0) > 0" in where


def test_los_cotizables_van_primero():
    """El modelo tiene tope de resultados: sin este orden, un recorte podría
    dejar fuera justo los que sí se pueden cotizar."""
    import inspect

    from packages.adapters.catalogo import CatalogoRepo  # noqa: PLC0415

    fuente = inspect.getsource(CatalogoRepo.buscar)
    assert "ORDER BY (r.precio_final IS NULL)" in fuente


def test_valorizar_exige_precio():
    """`precios_por_sku` alimenta la cotización: no puede colar SKUs sin precio."""
    import inspect

    from packages.adapters.catalogo import CatalogoRepo  # noqa: PLC0415

    fuente = inspect.getsource(CatalogoRepo.precios_por_sku)
    assert "r.precio_final IS NOT NULL" in fuente
