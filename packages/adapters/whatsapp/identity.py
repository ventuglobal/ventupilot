"""Hasheo de identificadores de WhatsApp para almacenamiento.

Invariante 8 del plan: nada de PII en logs ni en tablas donde no haga falta.

Un SHA256 pelado de un número de teléfono no es anonimización. Un móvil chileno
es +569 seguido de 8 dígitos: 10^8 candidatos, que una GPU agota en segundos.
Por eso se usa HMAC con un pepper del entorno, que un atacante con acceso a la
base de datos no tiene.
"""

from __future__ import annotations

import hashlib
import hmac


def hash_wa_id(wa_id: str, pepper: str) -> str:
    """Devuelve el hash estable de un `wa_id` de WhatsApp.

    Args:
        wa_id: número en formato internacional sin `+`, tal como lo manda Meta
            (ej. "56912345678").
        pepper: secreto del entorno (`WA_ID_PEPPER`). Rotarlo invalida todos
            los hashes almacenados, así que trátalo como parte del esquema.

    Raises:
        ValueError: si el pepper está vacío. Sin pepper el hash es reversible,
            y degradar en silencio a un hash inseguro es peor que fallar.
    """
    if not pepper:
        raise ValueError("WA_ID_PEPPER no configurado: el hash sería reversible")

    return hmac.new(
        key=pepper.encode("utf-8"),
        msg=wa_id.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()


def enmascarar(wa_id: str) -> str:
    """Versión legible para logs y mensajes de error: `56•••••678`.

    Suficiente para que un operador correlacione a mano sin dejar el número
    completo en el log.
    """
    if len(wa_id) <= 5:
        return "•" * len(wa_id)
    return f"{wa_id[:2]}{'•' * (len(wa_id) - 5)}{wa_id[-3:]}"
