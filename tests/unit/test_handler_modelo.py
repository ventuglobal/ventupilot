"""El handler tiene que pasarle un modelo al agente.

Este test existe por un fallo real en producción: el `Agent` se construyó sin
`model` y el handler tampoco lo pasaba, así que el primer mensaje de verdad
murió con `UserError: model must either be set on the agent or included when
calling it`.

Ningún test lo detectó porque **todos inyectaban `model=FunctionModel(...)`**.
El único camino que no recibía modelo era justo el de producción, y era el
único que nadie ejercitaba. De ahí que esto compruebe la llamada del handler
y no la del agente.
"""

from __future__ import annotations

from typing import Any

import pytest

from packages.adapters.config import Settings
from packages.agents import handler as handler_mod
from packages.agents.agente import RespuestaAgente
from packages.agents.handler import HandlerAgente
from packages.domain.identidad import ClienteAutorizado, Permiso


class _ResultadoFalso:
    def __init__(self) -> None:
        self.output = RespuestaAgente(mensaje="ok", lineas=[], proponer=False)

    def new_messages(self) -> list[Any]:
        return []


@pytest.fixture
def capturar_run(monkeypatch):
    """Sustituye `agente.run` y devuelve los kwargs con que se llamó."""
    capturado: dict[str, Any] = {}

    async def run_falso(*args: Any, **kwargs: Any) -> Any:
        capturado.update(kwargs)
        return _ResultadoFalso()

    monkeypatch.setattr(handler_mod.agente, "run", run_falso)

    # El historial no es lo que se prueba aquí; se anula para no tocar la base.
    async def sin_historial(*a: Any, **k: Any) -> list[Any]:
        return []

    async def no_anexar(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(handler_mod.conversaciones, "cargar_historial", sin_historial)
    monkeypatch.setattr(handler_mod.conversaciones, "anexar_historial", no_anexar)
    return capturado


def _handler(**extra: object) -> HandlerAgente:
    base: dict[str, object] = {
        "wa_app_secret": "x",
        "wa_verify_token": "x",
        "wa_id_pepper": "p" * 32,
        "database_url": "postgres://x",
    }
    base.update(extra)
    settings = Settings(**base)  # type: ignore[arg-type]
    return HandlerAgente(
        settings=settings,
        catalogo=None,  # type: ignore[arg-type]
        clientes=None,  # type: ignore[arg-type]
        propuestas=None,  # type: ignore[arg-type]
    )


async def _correr(h: HandlerAgente) -> None:
    from packages.adapters.whatsapp.payloads import MensajeEntrante, TipoMensaje

    mensaje = MensajeEntrante(
        wa_message_id="wamid.X",
        wa_id="56911112222",
        phone_number_id="PNID",
        tipo=TipoMensaje.TEXTO,
        texto="hola",
        recibido_at=__import__("datetime").datetime.now(
            tz=__import__("datetime").UTC
        ),
    )
    cliente = ClienteAutorizado(
        wa_id_hash="h", permisos=frozenset({Permiso.CONSULTAR, Permiso.COTIZAR})
    )
    await h._correr_agente(None, mensaje, "h", cliente, 1, "hola")  # type: ignore[arg-type]


async def test_el_handler_pasa_un_modelo(capturar_run):
    """Sin esto, el primer mensaje real muere con UserError."""
    await _correr(_handler())
    assert capturar_run.get("model"), "el handler debe pasar `model` a agente.run"


async def test_el_modelo_sale_de_la_configuracion(capturar_run):
    """Cambiar AGENT_MODEL por entorno tiene que surtir efecto."""
    await _correr(_handler(agent_model="openai:otro-modelo"))
    assert capturar_run["model"] == "openai:otro-modelo"


def test_agent_model_tiene_un_defecto_utilizable():
    """El defecto no puede ser vacío: sería el mismo UserError."""
    assert Settings(
        wa_app_secret="x",
        wa_verify_token="x",
        wa_id_pepper="p" * 32,
        database_url="postgres://x",
    ).agent_model
