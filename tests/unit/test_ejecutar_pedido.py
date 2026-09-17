"""Ejecutar el pedido: crear la orden real en ventu 1.0 al confirmar.

Dos niveles:

- El handler solo invoca `OrdenesRepo.crear` cuando el flag está activo, el
  botón es el de confirmar y la propuesta de verdad cambió de estado.
  Rechazar, una propuesta ya no disponible, o el flag apagado no deben tocar
  `ordenes` — no se ejecuta nada que el usuario no haya confirmado.
- `OrdenesRepo.crear` no crea una orden real si `ventupilot.ordenes` ya
  tenía una para esa propuesta (invariante 5: el `ON CONFLICT` la descarta
  antes de tocar `orders_order`).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from packages.adapters.config import Settings
from packages.adapters.ordenes import OrdenesRepo
from packages.adapters.whatsapp.payloads import MensajeEntrante, TipoMensaje
from packages.agents.handler import MSG_CONFIRMADA, MSG_NO_DISPONIBLE, HandlerAgente
from packages.agents.mensajes import PREFIJO_CONFIRMAR, PREFIJO_RECHAZAR
from packages.domain.propuesta import LineaPropuesta


def _settings(**extra: object) -> Settings:
    base: dict[str, object] = {
        "wa_app_secret": "x",
        "wa_verify_token": "x",
        "wa_id_pepper": "p" * 32,
        "database_url": "postgres://x",
    }
    base.update(extra)
    return Settings(**base)  # type: ignore[arg-type]


class _PropuestasFalso:
    def __init__(self, cambio: bool) -> None:
        self._cambio = cambio

    async def resolver(self, conn: Any, propuesta_id: str, estado: Any, *, wa_id_hash: str) -> bool:
        return self._cambio


class _OrdenesFalso:
    def __init__(self) -> None:
        self.llamadas: list[dict[str, Any]] = []

    async def crear(
        self,
        conn: Any,
        propuesta_id: str,
        *,
        wa_id_hash: str,
        wa_id: str,
        nombre_perfil: str | None,
    ) -> int:
        self.llamadas.append(
            {
                "propuesta_id": propuesta_id,
                "wa_id_hash": wa_id_hash,
                "wa_id": wa_id,
                "nombre_perfil": nombre_perfil,
            }
        )
        return 999


def _mensaje_boton(payload_id: str) -> MensajeEntrante:
    return MensajeEntrante(
        wa_message_id="wamid.BOTON",
        wa_id="56911112222",
        phone_number_id="PNID",
        tipo=TipoMensaje.INTERACTIVO,
        payload_id=payload_id,
        nombre_perfil="Juan Pérez",
        recibido_at=datetime.now(tz=UTC),
    )


def _handler(propuestas: Any, ordenes: Any, **extra: object) -> HandlerAgente:
    return HandlerAgente(
        settings=_settings(**extra),
        catalogo=None,  # type: ignore[arg-type]
        clientes=None,  # type: ignore[arg-type]
        propuestas=propuestas,
        ordenes=ordenes,
    )


async def test_flag_apagado_no_crea_la_orden():
    """Por defecto (CREAR_ORDEN_VENTU=false), confirmar no toca ventu 1.0."""
    ordenes = _OrdenesFalso()
    h = _handler(_PropuestasFalso(cambio=True), ordenes)
    mensaje = _mensaje_boton(f"{PREFIJO_CONFIRMAR}prop123")

    respuesta = await h._resolver_boton(None, mensaje, "h")  # type: ignore[arg-type]

    assert ordenes.llamadas == []
    assert respuesta == MSG_CONFIRMADA


async def test_flag_encendido_crea_la_orden_al_confirmar():
    ordenes = _OrdenesFalso()
    h = _handler(_PropuestasFalso(cambio=True), ordenes, crear_orden_ventu=True)
    mensaje = _mensaje_boton(f"{PREFIJO_CONFIRMAR}prop123")

    respuesta = await h._resolver_boton(None, mensaje, "hash-abc")  # type: ignore[arg-type]

    assert respuesta == MSG_CONFIRMADA
    assert ordenes.llamadas == [
        {
            "propuesta_id": "prop123",
            "wa_id_hash": "hash-abc",
            "wa_id": "56911112222",
            "nombre_perfil": "Juan Pérez",
        }
    ]


async def test_rechazar_no_crea_orden_aunque_el_flag_este_encendido():
    ordenes = _OrdenesFalso()
    h = _handler(_PropuestasFalso(cambio=True), ordenes, crear_orden_ventu=True)
    mensaje = _mensaje_boton(f"{PREFIJO_RECHAZAR}prop123")

    await h._resolver_boton(None, mensaje, "h")  # type: ignore[arg-type]

    assert ordenes.llamadas == []


async def test_propuesta_no_disponible_no_crea_orden():
    """Si `resolver` dice que no cambió nada, no hay pedido que ejecutar."""
    ordenes = _OrdenesFalso()
    h = _handler(_PropuestasFalso(cambio=False), ordenes, crear_orden_ventu=True)
    mensaje = _mensaje_boton(f"{PREFIJO_CONFIRMAR}prop123")

    respuesta = await h._resolver_boton(None, mensaje, "h")  # type: ignore[arg-type]

    assert ordenes.llamadas == []
    assert respuesta == MSG_NO_DISPONIBLE


# ── OrdenesRepo.crear ────────────────────────────────────────────────────────


class _FakeConn:
    """Doble mínimo de `asyncpg.Connection` para probar `OrdenesRepo.crear`
    sin base de datos: registra qué se ejecutó y responde con valores
    guionados, igual que haría Postgres para cada paso del flujo."""

    def __init__(
        self,
        *,
        snapshot_row: dict[str, Any] | None,
        id_ordenes: int | None,
        id_orders_order: int,
    ) -> None:
        self._snapshot_row = snapshot_row
        self._id_ordenes = id_ordenes
        self._id_orders_order = id_orders_order
        self.ejecutados: list[tuple[str, Any]] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.ejecutados.append(("fetchrow", args))
        assert "FROM ventupilot.propuestas" in query
        return self._snapshot_row

    async def fetchval(self, query: str, *args: Any) -> int | None:
        self.ejecutados.append(("fetchval", args))
        if "INSERT INTO ventupilot.ordenes" in query:
            return self._id_ordenes
        if "INSERT INTO orders_order" in query:
            return self._id_orders_order
        raise AssertionError(f"fetchval inesperado: {query!r}")

    async def executemany(self, query: str, params: list[tuple[Any, ...]]) -> None:
        self.ejecutados.append(("executemany", params))

    async def execute(self, query: str, *args: Any) -> None:
        self.ejecutados.append(("execute", args))


def _snapshot(lineas: list[LineaPropuesta], total: int) -> dict[str, Any]:
    return {
        "snapshot": json.dumps([ln.model_dump() for ln in lineas]),
        "total": Decimal(total),
    }


async def test_crea_la_orden_y_sus_items_en_orden():
    lineas = [
        LineaPropuesta(
            sku="A1", titulo="Taladro", cantidad=2, precio_unitario_clp=10_000, subtotal_clp=20_000
        ),
    ]
    conn = _FakeConn(
        snapshot_row=_snapshot(lineas, 20_000), id_ordenes=5, id_orders_order=777
    )
    repo = OrdenesRepo(pool=None)  # type: ignore[arg-type]

    resultado = await repo.crear(
        conn, "prop123", wa_id_hash="hash", wa_id="56911112222", nombre_perfil="Juan"
    )

    assert resultado == 777
    pasos = [tipo for tipo, _ in conn.ejecutados]
    assert pasos == ["fetchrow", "fetchval", "fetchval", "executemany", "execute"]

    _, items = conn.ejecutados[3]
    assert items == [(777, "A1", "Taladro", 2, Decimal(10_000))]

    _, args_update = conn.ejecutados[4]
    assert args_update == ("777", 5)


async def test_no_duplica_si_la_propuesta_ya_tenia_orden():
    """`ON CONFLICT DO NOTHING`: sin fila nueva en ventupilot.ordenes, no se
    toca orders_order — una orden real no puede quedar huérfana de esto."""
    lineas = [
        LineaPropuesta(
            sku="A1", titulo="Taladro", cantidad=1, precio_unitario_clp=1_000, subtotal_clp=1_000
        ),
    ]
    conn = _FakeConn(
        snapshot_row=_snapshot(lineas, 1_000), id_ordenes=None, id_orders_order=999
    )
    repo = OrdenesRepo(pool=None)  # type: ignore[arg-type]

    resultado = await repo.crear(
        conn, "prop123", wa_id_hash="hash", wa_id="56911112222", nombre_perfil=None
    )

    assert resultado is None
    pasos = [tipo for tipo, _ in conn.ejecutados]
    assert pasos == ["fetchrow", "fetchval"]


async def test_sin_propuesta_no_inventa_una_orden():
    """`fetchrow` sin fila (propuesta ajena o inexistente): no se escribe nada."""
    conn = _FakeConn(snapshot_row=None, id_ordenes=1, id_orders_order=1)
    repo = OrdenesRepo(pool=None)  # type: ignore[arg-type]

    resultado = await repo.crear(
        conn, "prop123", wa_id_hash="hash", wa_id="56911112222", nombre_perfil=None
    )

    assert resultado is None
    assert [tipo for tipo, _ in conn.ejecutados] == ["fetchrow"]
