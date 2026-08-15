"""El agente conversacional y sus tools.

Reparto de responsabilidades, que es lo que sostiene las invariantes 1 y 2:

    modelo  →  entiende el pedido, elige SKUs y cantidades
    backend →  busca precios, multiplica, arma la propuesta, la envía

El modelo nunca ve una operación aritmética sobre dinero ni ejecuta nada.
Su salida es `RespuestaAgente`: un texto y, si corresponde, una lista de
SKUs con cantidades. El worker toma esa lista, la valoriza contra el motor
de precios y construye la cotización.

Los precios aparecen en los resultados de búsqueda porque el modelo necesita
saberlos para elegir bien y para redactar. Que los vea no le permite
calcular con ellos: el total que llega al usuario lo produce
`construir_propuesta`, no el modelo.
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.usage import UsageLimits

from packages.agents.deps import DepsAgente
from packages.domain.catalogo import ResultadoBusqueda
from packages.domain.identidad import Permiso
from packages.domain.propuesta import SeleccionLinea

INSTRUCCIONES = """\
Eres el asistente de compras de Ventu por WhatsApp. Atiendes a clientes en \
Chile, en español, con el tono directo y práctico de un vendedor con \
oficio: cordial, sin adornos, sin emojis.

Cómo trabajas:

- Para saber qué hay y a qué precio, usa `buscar_productos`. Nunca cites un \
producto ni un precio de memoria: si no salió de la herramienta, no existe.
- Cuando el cliente concrete qué quiere y en qué cantidad, devuelve esas \
líneas en `lineas` y pon `proponer` en true. El sistema calcula el total y \
le manda la cotización con un botón para confirmar.
- Mientras estés entendiendo el pedido, deja `lineas` vacía y `proponer` en \
false. Conversa con normalidad.

Reglas que no puedes romper:

- No calcules totales, descuentos, impuestos ni plazos, ni siquiera de \
cabeza y ni siquiera si el cliente insiste. El sistema lo hace. Si te \
piden el total, propón las líneas y deja que llegue la cotización.
- No prometas plazos de entrega, condiciones de pago ni descuentos. No los \
conoces.
- No inventes SKUs. Usa exactamente los que te devolvió la búsqueda.
- Si no hay stock o no encuentras el producto, dilo claro y ofrece \
alternativas de la búsqueda si las hay.
- Mensajes cortos. Esto es WhatsApp: dos o tres frases, y una lista solo \
cuando comparas productos.
"""


class RespuestaAgente(BaseModel):
    """Salida estructurada del agente.

    `lineas` es deliberadamente `SeleccionLinea` (SKU + cantidad) y no algo
    con precio: el tipo hace imposible que el modelo devuelva dinero.
    """

    mensaje: str = Field(
        description="Texto que se le envía al cliente por WhatsApp. En español, breve."
    )
    lineas: list[SeleccionLinea] = Field(
        default_factory=list,
        description=(
            "SKUs y cantidades a cotizar. Vacía si todavía estás entendiendo el pedido."
        ),
    )
    proponer: bool = Field(
        default=False,
        description=(
            "true solo cuando el cliente ya concretó qué quiere y hay que "
            "mandarle la cotización formal para que la confirme."
        ),
    )


agente = Agent(
    deps_type=DepsAgente,
    output_type=RespuestaAgente,
    instructions=INSTRUCCIONES,
    # Un fallo de validación de la salida se reintenta una vez. Más que eso
    # multiplica el costo del turno sin mejorar el resultado.
    retries=1,
)


@agente.tool
async def buscar_productos(ctx: RunContext[DepsAgente], consulta: str) -> ResultadoBusqueda:
    """Busca productos disponibles en el catálogo de Ventu.

    Devuelve solo productos activos, con stock y con precio vigente. Úsala
    siempre antes de mencionar un producto o un precio.

    Args:
        consulta: qué buscar. Nombre, modelo, marca o parte del SKU. Prefiere
            términos concretos ("notebook lenovo 14", "SSD 1TB") a frases
            largas: la búsqueda es por coincidencia de texto, no semántica.
    """
    texto = consulta.strip()
    if len(texto) < 2:
        # ModelRetry le devuelve el problema al modelo para que reformule,
        # en vez de gastar una consulta que traería medio catálogo.
        raise ModelRetry("La consulta es demasiado corta. Usa al menos dos caracteres.")

    cfg = ctx.deps.settings
    return await ctx.deps.catalogo.buscar(
        texto,
        canal=cfg.pricing_channel,
        max_edad_horas=cfg.precio_max_edad_horas,
        limite=cfg.max_resultados_busqueda,
    )


@agente.instructions
def contexto_del_cliente(ctx: RunContext[DepsAgente]) -> str:
    """Inyecta quién es el cliente y qué puede hacer.

    Va como instrucción y no como parte del prompt del usuario para que el
    modelo no lo confunda con algo que el remitente escribió y, por tanto,
    no acepte que se lo contradigan desde el chat.
    """
    cliente = ctx.deps.cliente
    nombre = cliente.nombre or "cliente"

    if not cliente.puede(Permiso.COTIZAR):
        return (
            f"Estás hablando con {nombre}, que NO está autorizado a recibir "
            "cotizaciones. Puedes responder dudas generales sobre productos, "
            "pero no propongas líneas ni pongas `proponer` en true bajo "
            "ninguna circunstancia. Si insiste en comprar, dile que un "
            "ejecutivo de Ventu se pondrá en contacto."
        )

    return f"Estás hablando con {nombre}, que sí está autorizado a recibir cotizaciones."


def limites(cfg: object) -> UsageLimits:
    """Límites duros de un run (invariante 7).

    Un bucle de tool calls contra un catálogo grande puede quemar bastante
    dinero antes de que nadie lo note. Estos topes cortan el run con
    `UsageLimitExceeded`, que el worker convierte en una disculpa al usuario
    en vez de en un silencio.
    """
    return UsageLimits(
        request_limit=getattr(cfg, "max_requests_per_run", 6),
        tool_calls_limit=getattr(cfg, "max_tool_calls_per_run", 12),
    )
