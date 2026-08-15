"""Tests del handler de eco del worker.

El bucle y las transacciones se prueban contra Postgres real en
tests/integration; aquí se verifica la política de respuesta, que es la parte
que tiene decisiones de producto dentro.

El eco sobrevive a la Fase 2 como herramienta de diagnóstico: si el eco llega
y el agente no, el problema no está en Meta. Desde la Fase 2 devuelve cuerpos
de Graph API en vez de texto, porque una respuesta puede necesitar botones.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from packages.adapters.whatsapp.payloads import MensajeEntrante, TipoMensaje
from services.agent_worker.main import RESPUESTA_NO_SOPORTADO, handler_eco

HASH = "hash-de-prueba"


def _mensaje(tipo: TipoMensaje, texto: str | None = None) -> MensajeEntrante:
    return MensajeEntrante(
        wa_message_id="wamid.X",
        wa_id="56912345678",
        phone_number_id="PNID",
        tipo=tipo,
        texto=texto,
        recibido_at=datetime.now(tz=UTC),
    )


def _texto(cuerpos: list[dict[str, Any]]) -> str:
    """Extrae el texto del único cuerpo devuelto."""
    assert len(cuerpos) == 1
    return str(cuerpos[0]["text"]["body"])


async def test_eco_devuelve_el_texto() -> None:
    # conn no se usa en el eco: no toca la base.
    cuerpos = await handler_eco(None, _mensaje(TipoMensaje.TEXTO, "quiero 200 ampolletas"), HASH)  # type: ignore[arg-type]
    assert _texto(cuerpos) == "recibí: quiero 200 ampolletas"


async def test_nota_de_voz_recibe_respuesta_util_no_silencio() -> None:
    """El silencio deja al cliente sin saber si el sistema está caído.

    Es una decisión de producto explícita: v1 no procesa audio, pero lo dice.
    """
    cuerpos = await handler_eco(None, _mensaje(TipoMensaje.NO_SOPORTADO), HASH)  # type: ignore[arg-type]
    assert _texto(cuerpos) == RESPUESTA_NO_SOPORTADO


async def test_imagen_con_caption_tambien_se_rechaza_explicitamente() -> None:
    """El caption trae intención, pero v1 no puede ver la imagen que la sustenta."""
    cuerpos = await handler_eco(  # type: ignore[arg-type]
        None, _mensaje(TipoMensaje.NO_SOPORTADO, "cotiza esto x100"), HASH
    )
    assert _texto(cuerpos) == RESPUESTA_NO_SOPORTADO


async def test_texto_vacio_no_genera_respuesta() -> None:
    assert await handler_eco(None, _mensaje(TipoMensaje.TEXTO, None), HASH) == []  # type: ignore[arg-type]


async def test_boton_devuelve_eco_del_titulo() -> None:
    cuerpos = await handler_eco(None, _mensaje(TipoMensaje.INTERACTIVO, "Confirmar"), HASH)  # type: ignore[arg-type]
    assert _texto(cuerpos) == "recibí: Confirmar"
