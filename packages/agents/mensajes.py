"""Render de la cotización para WhatsApp.

Esta es la función que "escribe el dinero", y por eso vive aquí y no en el
modelo (invariante 1). El modelo nunca ve este texto antes de que salga.

Los botones llevan el `propuesta_id` dentro de su id. Eso es lo que hace que
la confirmación sea estructurada y no texto libre (invariante 2): un "sí"
escrito es ambiguo cuando hay dos cotizaciones abiertas; el id de un botón
no lo es.
"""

from __future__ import annotations

from typing import Any

from packages.domain.propuesta import Propuesta

# Meta rechaza el mensaje si un título de botón pasa de 20 caracteres, y
# trunca el cuerpo a 1024. Truncamos nosotros para que un título largo no
# tire el envío entero.
_MAX_TITULO_BOTON = 20
_MAX_CUERPO = 1024

PREFIJO_CONFIRMAR = "conf:"
PREFIJO_RECHAZAR = "rech:"


def formatear_clp(monto: int) -> str:
    """Formatea pesos chilenos: 1234567 → "$1.234.567".

    Punto como separador de miles, que es la convención chilena, y sin
    decimales porque el peso no los usa en circulación.
    """
    return "$" + f"{monto:,}".replace(",", ".")


def texto_propuesta(propuesta: Propuesta) -> str:
    """Renderiza la cotización como texto."""
    lineas = [
        f"• {ln.cantidad} × {ln.titulo}\n  {formatear_clp(ln.precio_unitario_clp)} c/u "
        f"= {formatear_clp(ln.subtotal_clp)}"
        for ln in propuesta.lineas
    ]
    cuerpo = "\n".join(lineas)
    return (
        f"{cuerpo}\n\n*Total: {formatear_clp(propuesta.total_clp)}*\n\n"
        "Los precios pueden cambiar. Confirma para reservar."
    )[:_MAX_CUERPO]


def cuerpo_propuesta(wa_id: str, propuesta: Propuesta) -> dict[str, Any]:
    """Cuerpo de Graph API para la cotización, con botones.

    Devuelve el dict tal cual lo espera el outbox, en la misma forma que
    `cuerpo_texto` de `adapters.whatsapp.client`.
    """
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": wa_id,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": texto_propuesta(propuesta)},
            "action": {
                "buttons": [
                    {
                        "type": "reply",
                        "reply": {
                            "id": f"{PREFIJO_CONFIRMAR}{propuesta.propuesta_id}",
                            "title": "Confirmar"[:_MAX_TITULO_BOTON],
                        },
                    },
                    {
                        "type": "reply",
                        "reply": {
                            "id": f"{PREFIJO_RECHAZAR}{propuesta.propuesta_id}",
                            "title": "No, gracias"[:_MAX_TITULO_BOTON],
                        },
                    },
                ]
            },
        },
    }
