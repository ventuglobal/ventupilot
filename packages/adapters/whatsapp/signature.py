"""Verificación de la firma HMAC del webhook de WhatsApp Cloud API.

Meta firma cada POST con HMAC-SHA256 del cuerpo usando el app secret, y lo
manda en la cabecera `X-Hub-Signature-256` con el formato `sha256=<hex>`.

El error clásico —y la razón por la que este módulo existe aislado y con
tests— es calcular el HMAC sobre el cuerpo ya parseado y vuelto a serializar.
El JSON de Meta no round-trippea byte a byte: cambia el orden de las claves,
el espaciado y el escapado de no-ASCII. La firma tiene que calcularse sobre
los bytes exactos que llegaron por el socket.
"""

from __future__ import annotations

import hashlib
import hmac

SIGNATURE_HEADER = "X-Hub-Signature-256"
_PREFIX = "sha256="


def firmar(body: bytes, app_secret: str) -> str:
    """Devuelve la cabecera de firma que Meta enviaría para `body`.

    Se usa en los tests y para firmar peticiones salientes en entornos de
    prueba. En producción solo se llama indirectamente desde `verificar_firma`.
    """
    digest = hmac.new(
        key=app_secret.encode("utf-8"),
        msg=body,
        digestmod=hashlib.sha256,
    ).hexdigest()
    return f"{_PREFIX}{digest}"


def verificar_firma(body: bytes, header: str | None, app_secret: str) -> bool:
    """Verifica la firma de un webhook entrante.

    Args:
        body: cuerpo crudo de la petición, tal como llegó. NO uses el JSON
            re-serializado: la firma no coincidirá.
        header: valor de `X-Hub-Signature-256`, o None si no venía.
        app_secret: app secret de la app de Meta.

    Returns:
        True solo si la firma es válida. Cualquier anomalía (cabecera ausente,
        prefijo incorrecto, hex inválido, secret vacío) devuelve False en vez
        de lanzar: un atacante no debe poder distinguir modos de fallo, y una
        excepción no controlada aquí tumbaría el endpoint.
    """
    if not app_secret:
        # Sin secret no hay verificación posible. Fallar cerrado, nunca abierto.
        return False
    if not header or not header.startswith(_PREFIX):
        return False

    recibido = header[len(_PREFIX) :]
    if not recibido:
        return False

    esperado = firmar(body, app_secret)[len(_PREFIX) :]

    # compare_digest evita filtrar información por el tiempo de comparación.
    # Comparamos en minúsculas porque el hex de Meta podría llegar en cualquier
    # caja; compare_digest sí distingue mayúsculas.
    return hmac.compare_digest(recibido.lower(), esperado)


def verificar_handshake(
    modo: str | None,
    token: str | None,
    challenge: str | None,
    verify_token: str,
) -> str | None:
    """Resuelve el handshake de verificación (GET /webhook).

    Meta llama una vez con `hub.mode=subscribe`, `hub.verify_token` y
    `hub.challenge`. Hay que devolver el challenge tal cual, como texto plano,
    y solo si el token coincide.

    Returns:
        El challenge si la verificación es correcta, None en caso contrario.
    """
    if modo != "subscribe" or not verify_token:
        return None
    if not token or not hmac.compare_digest(token, verify_token):
        return None
    return challenge
