"""Modelos de negocio, sin I/O.

Este paquete no importa nada de `packages.adapters`. Esa frontera es lo que
permite testear la lógica —valorizar una propuesta, decidir un permiso— sin
levantar Postgres ni hablar con Meta.
"""

from packages.domain.catalogo import ProductoDisponible, ResultadoBusqueda
from packages.domain.identidad import DESCONOCIDO, ClienteAutorizado, Permiso
from packages.domain.propuesta import (
    EstadoPropuesta,
    LineaPropuesta,
    Propuesta,
    SeleccionLinea,
    construir_propuesta,
    id_propuesta,
)

__all__ = [
    "DESCONOCIDO",
    "ClienteAutorizado",
    "EstadoPropuesta",
    "LineaPropuesta",
    "Permiso",
    "ProductoDisponible",
    "Propuesta",
    "ResultadoBusqueda",
    "SeleccionLinea",
    "construir_propuesta",
    "id_propuesta",
]
