"""La expresión de búsqueda del catálogo, en un solo sitio.

Vive aparte porque **tiene que ser idéntica byte a byte** en dos lugares: en el
`WHERE` de la consulta y en la definición del índice de expresión. Postgres
solo usa un índice de expresión cuando la expresión de la consulta coincide
con la indexada; una coma de más y el planificador vuelve al recorrido
secuencial sin decir nada. El síntoma sería el de ahora —lentitud— y no un
error, así que nadie se enteraría.

Por qué una concatenación y no cinco columnas: la búsqueda es un `OR` sobre
título, SKU, SKU Ventu, modelo y part number. Con cinco índices separados,
Postgres tendría que hacer cinco búsquedas y unirlas; con uno sobre la
concatenación, hace una. En 28k filas la diferencia ya se nota, y `ILIKE
'%x%'` sobre el texto unido es equivalente a buscar en cualquiera de los
campos.
"""

from __future__ import annotations

# Alias `p` porque así aparece la tabla en las consultas del catálogo.
EXPRESION_BUSQUEDA = (
    "(coalesce(p.title,'') || ' ' || coalesce(p.sku,'') || ' ' "
    "|| coalesce(p.ventu_sku,'') || ' ' || coalesce(p.model,'') || ' ' "
    "|| coalesce(p.part_number,''))"
)

# La misma expresión con el alias resuelto, para el CREATE INDEX. Se deriva de
# la anterior en vez de escribirse dos veces: duplicarla a mano es justo cómo
# se rompe la coincidencia que hace que el índice sirva.
EXPRESION_INDICE = EXPRESION_BUSQUEDA.replace("p.", "")

NOMBRE_INDICE = "ix_productbase_busqueda_trgm"
