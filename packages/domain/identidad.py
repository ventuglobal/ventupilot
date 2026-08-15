"""Quién es quien escribe, y qué se le permite hacer.

Invariante 4: el número de teléfono es identidad, no autorización. Saber que
un mensaje viene de +569… dice quién escribe, no que pueda cotizar a nombre
de una empresa ni generar un pedido.

Por qué esto no se resuelve mirando `orders_customer` de ventu 1.0: esa
tabla es un punto de entrega, deduplicado por RUT y dirección. Su columna
`phone` no es única, no tiene índice y se llena con texto que llega de
MercadoLibre y Shopify —datos de terceros, no credenciales—. Autorizar por
coincidencia de teléfono contra esa tabla convertiría un dato scrapeado en
un permiso. Por eso ventupilot mantiene su propio registro, poblado a
propósito, y opcionalmente enlazado a un `orders_customer.id` para saber a
quién factura.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class Permiso(StrEnum):
    """Capacidades que se conceden por separado.

    Están separadas porque el salto de cotizar a comprometer un pedido es
    exactamente donde aparece el impacto económico, y no queremos que dar lo
    primero implique lo segundo.
    """

    CONSULTAR = "consultar"
    COTIZAR = "cotizar"
    PEDIR = "pedir"


class ClienteAutorizado(BaseModel):
    """Un remitente conocido y lo que se le permite.

    `wa_id_hash` es la clave: el número en claro no se guarda. `customer_id`
    apunta a `orders_customer` de ventu 1.0 cuando se sabe a qué cliente
    corresponde; es opcional porque un contacto puede estar autorizado a
    consultar el catálogo antes de existir como cliente facturable.
    """

    wa_id_hash: str
    customer_id: int | None = None
    nombre: str | None = None
    permisos: frozenset[Permiso] = Field(default_factory=frozenset)
    activo: bool = True

    def puede(self, permiso: Permiso) -> bool:
        """Una sola puerta para preguntar. Un cliente inactivo no puede nada.

        Que `activo=False` corte todo, en vez de vaciar la lista de permisos,
        permite desactivar a alguien temporalmente sin perder qué tenía
        concedido cuando vuelva.
        """
        return self.activo and permiso in self.permisos


# Remitente sin registro. No es un error: es el caso común de alguien que
# escribe por primera vez. Puede consultar nada y recibe una respuesta que
# lo deriva a un humano.
DESCONOCIDO = ClienteAutorizado(wa_id_hash="", permisos=frozenset(), activo=False)
