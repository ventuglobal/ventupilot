"""Tests del agente con un modelo de prueba.

Se usa `FunctionModel` de pydantic-ai, que sustituye al LLM por una función
nuestra. Así se prueba el cableado —tools registradas, deps que llegan,
salida estructurada, límites— sin clave de API, sin red y sin costo, que es
lo que permite que estos tests corran en cada PR.

Lo que estos tests NO cubren es la calidad de las respuestas del modelo real;
eso necesita evaluaciones aparte.
"""

from __future__ import annotations

import pytest
from pydantic_ai import UnexpectedModelBehavior
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from packages.adapters.config import Settings
from packages.agents.agente import RespuestaAgente, agente, limites
from packages.agents.deps import DepsAgente
from packages.domain.identidad import ClienteAutorizado, Permiso


def _settings(**extra: object) -> Settings:
    """Settings mínimos. Los campos obligatorios tienen validación de longitud."""
    base: dict[str, object] = {
        "wa_app_secret": "x",
        "wa_verify_token": "x",
        "wa_id_pepper": "p" * 32,
        "database_url": "postgres://x",
    }
    base.update(extra)
    return Settings(**base)  # type: ignore[arg-type]


def _deps(permisos: set[Permiso]) -> DepsAgente:
    """Deps con catálogo nulo: ninguno de estos tests llega a la base."""
    return DepsAgente(
        catalogo=None,  # type: ignore[arg-type]
        settings=_settings(),
        wa_id_hash="hash-de-prueba",
        cliente=ClienteAutorizado(
            wa_id_hash="hash-de-prueba",
            nombre="Ana",
            permisos=frozenset(permisos),
        ),
        wa_message_id="wamid.test",
    )


def _responder(mensaje: str, lineas: list[dict[str, object]], proponer: bool):
    """Construye una función-modelo que devuelve una salida fija."""

    def f(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        salida = RespuestaAgente(
            mensaje=mensaje,
            lineas=lineas,  # type: ignore[arg-type]
            proponer=proponer,
        )
        assert info.output_tools is not None
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name=info.output_tools[0].name,
                    args=salida.model_dump(),
                )
            ]
        )

    return f


async def test_devuelve_salida_estructurada():
    resultado = await agente.run(
        "hola",
        deps=_deps({Permiso.CONSULTAR, Permiso.COTIZAR}),
        model=FunctionModel(_responder("Hola Ana, ¿qué necesitas?", [], False)),
    )
    assert isinstance(resultado.output, RespuestaAgente)
    assert resultado.output.proponer is False
    assert resultado.output.lineas == []


async def test_la_seleccion_no_admite_precios():
    """Invariante 1 sostenida por el tipo: no hay dónde poner un monto."""
    campos = set(RespuestaAgente.model_fields["lineas"].annotation.__args__[0].model_fields)  # type: ignore[union-attr]
    assert campos == {"sku", "cantidad"}


async def test_la_tool_de_busqueda_esta_registrada():
    """El modelo tiene que poder consultar el catálogo; si no, inventa."""
    nombres: list[str] = []

    def capturar(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nombres.extend(t.name for t in info.function_tools)
        return ModelResponse(parts=[TextPart("ok")])

    with pytest.raises(UnexpectedModelBehavior):
        # Devolver texto donde se espera salida estructurada hace que el
        # agente reintente y acabe fallando. No importa: lo que se comprueba
        # es qué tools vio el modelo.
        await agente.run(
            "hola",
            deps=_deps({Permiso.CONSULTAR}),
            model=FunctionModel(capturar),
        )

    assert "buscar_productos" in nombres


async def test_instruccion_distinta_para_quien_no_puede_cotizar():
    """Un cliente sin permiso de cotizar recibe instrucciones que lo dicen."""
    capturado: list[str] = []

    def capturar(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        capturado.append(messages[0].instructions or "")
        salida = RespuestaAgente(mensaje="ok", lineas=[], proponer=False)
        assert info.output_tools is not None
        return ModelResponse(
            parts=[ToolCallPart(tool_name=info.output_tools[0].name, args=salida.model_dump())]
        )

    await agente.run(
        "quiero comprar",
        deps=_deps({Permiso.CONSULTAR}),
        model=FunctionModel(capturar),
    )
    assert "NO está autorizado" in capturado[0]


async def test_instruccion_de_cliente_autorizado():
    capturado: list[str] = []

    def capturar(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        capturado.append(messages[0].instructions or "")
        salida = RespuestaAgente(mensaje="ok", lineas=[], proponer=False)
        assert info.output_tools is not None
        return ModelResponse(
            parts=[ToolCallPart(tool_name=info.output_tools[0].name, args=salida.model_dump())]
        )

    await agente.run(
        "quiero comprar",
        deps=_deps({Permiso.CONSULTAR, Permiso.COTIZAR}),
        model=FunctionModel(capturar),
    )
    assert "sí está autorizado" in capturado[0]


def test_limites_salen_de_la_config():
    """Invariante 7: los topes de un run son configuración, no constantes."""
    cfg = _settings(max_requests_per_run=3, max_tool_calls_per_run=7)
    lim = limites(cfg)
    assert lim.request_limit == 3
    assert lim.tool_calls_limit == 7
