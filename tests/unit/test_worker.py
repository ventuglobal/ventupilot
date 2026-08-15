"""Tests del handler del worker.

El bucle y las transacciones se prueban contra Postgres real en
tests/integration; aquí se verifica la política de respuesta, que es la parte
que tiene decisiones de producto dentro.
"""

from __future__ import annotations

from datetime import UTC, datetime

from packages.adapters.whatsapp.payloads import MensajeEntrante, TipoMensaje
from services.agent_worker.main import RESPUESTA_NO_SOPORTADO, handler_eco


def _mensaje(tipo: TipoMensaje, texto: str | None = None) -> MensajeEntrante:
    return MensajeEntrante(
        wa_message_id="wamid.X",
        wa_id="56912345678",
        phone_number_id="PNID",
        tipo=tipo,
        texto=texto,
        recibido_at=datetime.now(tz=UTC),
    )


async def test_eco_devuelve_el_texto() -> None:
    respuesta = await handler_eco(_mensaje(TipoMensaje.TEXTO, "quiero 200 ampolletas"))
    assert respuesta == "recibí: quiero 200 ampolletas"


async def test_nota_de_voz_recibe_respuesta_util_no_silencio() -> None:
    """El silencio deja al cliente sin saber si el sistema está caído.

    Es una decisión de producto explícita: v1 no procesa audio, pero lo dice.
    """
    respuesta = await handler_eco(_mensaje(TipoMensaje.NO_SOPORTADO))
    assert respuesta == RESPUESTA_NO_SOPORTADO


async def test_imagen_con_caption_tambien_se_rechaza_explicitamente() -> None:
    """El caption trae intención, pero v1 no puede ver la imagen que la sustenta."""
    respuesta = await handler_eco(
        _mensaje(TipoMensaje.NO_SOPORTADO, "cotiza esto x100")
    )
    assert respuesta == RESPUESTA_NO_SOPORTADO


async def test_texto_vacio_no_genera_respuesta() -> None:
    assert await handler_eco(_mensaje(TipoMensaje.TEXTO, None)) is None


async def test_boton_devuelve_eco_del_titulo() -> None:
    respuesta = await handler_eco(_mensaje(TipoMensaje.INTERACTIVO, "Confirmar"))
    assert respuesta == "recibí: Confirmar"
