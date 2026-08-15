"""Dependencias de un run del agente.

Invariante 3: la identidad viene de aquí, no de argumentos del modelo.

No es una preferencia de estilo. Si el `wa_id_hash` fuera parámetro de una
tool, el modelo podría escribirlo —y un usuario podría convencerlo de
escribir el de otro—. Al vivir en `deps`, el modelo no puede nombrarlo: las
tools lo leen de `ctx.deps`, que construye el worker a partir de un mensaje
ya verificado por HMAC.

Por la misma razón `cliente` viaja resuelto y no se consulta dentro de una
tool: los permisos se deciden antes de que el modelo hable.

El repositorio de catálogo llega ya construido sobre el pool de solo
lectura. El agente no tiene forma de alcanzar el pool de escritura.
"""

from __future__ import annotations

from dataclasses import dataclass

from packages.adapters.catalogo import CatalogoRepo
from packages.adapters.config import Settings
from packages.domain.identidad import ClienteAutorizado


@dataclass(slots=True)
class DepsAgente:
    """Todo lo que las tools necesitan y el modelo no puede tocar."""

    catalogo: CatalogoRepo
    settings: Settings
    # Identidad ya verificada. No es opcional: un run sin remitente resuelto
    # no debería llegar a crearse.
    wa_id_hash: str
    cliente: ClienteAutorizado
    # Mensaje que originó el run. Entra en la clave determinística de la
    # propuesta, así que un reproceso genera el mismo id (invariante 5).
    wa_message_id: str
