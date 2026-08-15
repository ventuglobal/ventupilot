"""Modelos de catálogo y precio. Sin I/O.

Un `ProductoDisponible` es lo que el agente puede ofrecer: existe, tiene
stock y tiene un precio vigente calculado por el motor de precios de
ventu 1.0. Un producto sin precio vigente no es un producto ofrecible, así
que no hay estado intermedio "producto sin precio" en este módulo: o entra
completo, o no entra.

Los precios son enteros de pesos chilenos. El peso chileno no tiene
subdivisión en circulación, y el motor de precios de ventu 1.0 ya trabaja en
enteros (`precio_final` es un IntegerField). Usar float aquí introduciría
error de redondeo en una cifra que el usuario va a leer literalmente.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ProductoDisponible(BaseModel):
    """Un producto ofrecible, con su precio ya resuelto por el backend.

    Este es el objeto que ve el modelo. Deliberadamente **no** trae costo,
    margen ni política de precios: el agente no negocia márgenes y esos
    datos no deben poder filtrarse a un mensaje de WhatsApp.
    """

    sku: str
    ventu_sku: str | None = None
    titulo: str
    marca: str | None = None
    stock: int = Field(ge=0)
    # Precio final con el que se cotiza, en CLP enteros. Sale del motor de
    # precios; el modelo nunca lo calcula ni lo ajusta (invariante 1).
    precio_clp: int = Field(ge=0)
    canal: str
    # Cuándo lo calculó el motor. El backend descarta lo rancio antes de
    # construir este objeto, pero se conserva para poder auditar una
    # cotización a posteriori.
    precio_calculado_at: datetime


class ResultadoBusqueda(BaseModel):
    """Lo que devuelve la tool de búsqueda.

    Incluye `total_encontrados` aparte de la lista porque el modelo redacta
    mejor cuando sabe que está viendo un recorte ("encontré 40, te muestro
    los 8 más relevantes") en vez de asumir que vio todo el catálogo.
    """

    productos: list[ProductoDisponible] = Field(default_factory=list)
    total_encontrados: int = 0
    truncado: bool = False
