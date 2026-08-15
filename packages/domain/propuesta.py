"""Propuestas: lo que el agente sugiere y el usuario confirma.

Aquí vive la invariante 2. El agente produce una `Seleccion` —SKUs y
cantidades, nada más— y es el backend quien busca los precios, arma la
`Propuesta` y calcula el total. El modelo nunca ve la aritmética ni la
ejecuta: propone, y un elemento estructurado de WhatsApp la confirma.

La separación entre `Seleccion` y `Propuesta` es justamente esa frontera.
Si algún día alguien hace que el modelo devuelva un total, el tipo no
compila.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, Field


class EstadoPropuesta(StrEnum):
    """Ciclo de vida de una propuesta.

    Los valores coinciden con los que escribe `migrations/001_inicial.sql`
    (`estado TEXT NOT NULL DEFAULT 'vigente'`).
    """

    VIGENTE = "vigente"
    CONFIRMADA = "confirmada"
    RECHAZADA = "rechazada"
    # No la rechazó el usuario: se le pasó el TTL. Se distingue de RECHAZADA
    # porque una expirada se puede volver a ofrecer y una rechazada no.
    EXPIRADA = "expirada"


class SeleccionLinea(BaseModel):
    """Una línea tal como la propone el modelo: qué y cuánto. Sin dinero."""

    sku: str = Field(min_length=1)
    cantidad: int = Field(gt=0, le=10_000)


class LineaPropuesta(BaseModel):
    """Una línea ya valorizada por el backend."""

    sku: str
    titulo: str
    cantidad: int = Field(gt=0)
    precio_unitario_clp: int = Field(ge=0)
    subtotal_clp: int = Field(ge=0)


class Propuesta(BaseModel):
    """Una cotización concreta, valorizada y con vencimiento.

    `propuesta_id` es determinístico (ver `id_propuesta`): reprocesar el
    mismo mensaje del mismo usuario con la misma selección produce el mismo
    id, y el `INSERT ... ON CONFLICT DO NOTHING` del repositorio lo vuelve
    idempotente (invariante 5). Sin eso, un reintento del worker duplicaría
    la propuesta y el usuario recibiría dos cotizaciones idénticas.
    """

    propuesta_id: str
    wa_id_hash: str
    lineas: list[LineaPropuesta]
    total_clp: int = Field(ge=0)
    estado: EstadoPropuesta = EstadoPropuesta.VIGENTE
    creada_at: datetime
    expira_at: datetime

    def vencida(self, ahora: datetime | None = None) -> bool:
        return (ahora or datetime.now(tz=UTC)) >= self.expira_at


def id_propuesta(wa_id_hash: str, wa_message_id: str, lineas: list[SeleccionLinea]) -> str:
    """Clave determinística de una propuesta.

    Depende del mensaje que la originó y del contenido exacto de la
    selección. Dos mensajes distintos con el mismo carrito son propuestas
    distintas —el usuario pidió lo mismo dos veces, y eso es legítimo—,
    pero un reintento del mismo mensaje no lo es.

    Las líneas se ordenan antes de hashear para que el mismo carrito en
    distinto orden no genere ids distintos.
    """
    partes = sorted(f"{ln.sku}:{ln.cantidad}" for ln in lineas)
    material = "|".join([wa_id_hash, wa_message_id, *partes])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def construir_propuesta(
    wa_id_hash: str,
    wa_message_id: str,
    seleccion: list[SeleccionLinea],
    precios: dict[str, tuple[str, int]],
    ttl_min: int,
    ahora: datetime | None = None,
) -> Propuesta:
    """Valoriza una selección del modelo y devuelve la propuesta.

    Esta función es el único lugar donde se multiplica cantidad por precio.
    Está en `domain/` y no toma dependencias de I/O justamente para que sea
    testeable sin base de datos y auditable de un vistazo.

    Args:
        seleccion: lo que devolvió el modelo.
        precios: mapa `sku → (titulo, precio_unitario_clp)` que el backend
            resolvió contra el motor de precios. Un SKU ausente aquí es un
            SKU que el modelo inventó o que dejó de tener precio vigente
            entre la búsqueda y la confirmación.
        ttl_min: minutos de validez.

    Raises:
        ValueError: si la selección está vacía o si algún SKU no tiene
            precio. Fallar es correcto: cotizar un SKU inventado a precio
            cero sería peor que no responder.
    """
    if not seleccion:
        raise ValueError("no se puede construir una propuesta vacía")

    faltantes = [ln.sku for ln in seleccion if ln.sku not in precios]
    if faltantes:
        raise ValueError(f"SKUs sin precio vigente: {', '.join(sorted(faltantes))}")

    creada = ahora or datetime.now(tz=UTC)
    lineas = []
    for ln in seleccion:
        titulo, unitario = precios[ln.sku]
        lineas.append(
            LineaPropuesta(
                sku=ln.sku,
                titulo=titulo,
                cantidad=ln.cantidad,
                precio_unitario_clp=unitario,
                subtotal_clp=unitario * ln.cantidad,
            )
        )

    return Propuesta(
        propuesta_id=id_propuesta(wa_id_hash, wa_message_id, seleccion),
        wa_id_hash=wa_id_hash,
        lineas=lineas,
        total_clp=sum(ln.subtotal_clp for ln in lineas),
        creada_at=creada,
        expira_at=creada + timedelta(minutes=ttl_min),
    )
