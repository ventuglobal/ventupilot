"""wa-gateway — recibe webhooks de WhatsApp, los verifica y los encola.

Deliberadamente tonto: no importa nada de `packages.agents` y no sabe qué es un
producto. Su único trabajo es decidir si un POST es auténtico, deduplicarlo y
dejarlo en la cola rápido. Toda la lógica de negocio vive en el worker.

Sobre los códigos de respuesta — Meta reintenta ante cualquier cosa que no sea
2xx, así que el código no es cosmético:

- Firma inválida → 403. Nunca queremos que reintente; o es un atacante o el
  secret está mal configurado, y en ninguno de los dos casos ayuda insistir.
- Payload sin mensajes procesables → 200. Reintentar no lo va a mejorar.
- Fallo al persistir → 503. Aquí sí queremos el reintento: es la diferencia
  entre perder el mensaje de un cliente y atenderlo un minuto más tarde.

Ese último punto matiza el invariante "responde 200 siempre". Devolver 200 con
la base caída significa perder mensajes en silencio, que es peor que el minuto
de latencia que cuesta el reintento de Meta.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from packages.adapters.cola import ColaRepo
from packages.adapters.config import Settings, get_settings
from packages.adapters.whatsapp.identity import enmascarar, hash_wa_id
from packages.adapters.whatsapp.payloads import parsear_webhook
from packages.adapters.whatsapp.signature import verificar_firma, verificar_handshake

log = logging.getLogger("wa_gateway")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = get_settings()
    logging.basicConfig(level=settings.log_level)

    pool = await asyncpg.create_pool(
        settings.database_url,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
    )
    app.state.pool = pool
    app.state.cola = ColaRepo(pool)
    app.state.settings = settings
    try:
        yield
    finally:
        await pool.close()


app = FastAPI(title="ventupilot wa-gateway", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"estado": "ok"}


@app.get("/webhook")
async def verificar(request: Request) -> Response:
    """Handshake de verificación. Meta lo llama una vez al configurar el webhook.

    Hay que devolver el `hub.challenge` como texto plano, sin comillas y sin
    envolverlo en JSON. Un 200 con `"1234"` entre comillas hace que Meta rechace
    la suscripción sin explicar por qué.
    """
    settings: Settings = request.app.state.settings
    challenge = verificar_handshake(
        modo=request.query_params.get("hub.mode"),
        token=request.query_params.get("hub.verify_token"),
        challenge=request.query_params.get("hub.challenge"),
        verify_token=settings.wa_verify_token,
    )
    if challenge is None:
        log.warning("handshake rechazado: verify_token no coincide")
        return PlainTextResponse("forbidden", status_code=403)
    return PlainTextResponse(challenge, status_code=200)


@app.post("/webhook")
async def recibir(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
) -> Response:
    settings: Settings = request.app.state.settings
    cola: ColaRepo = request.app.state.cola

    # El cuerpo crudo, antes de cualquier parseo. La firma se calcula sobre
    # estos bytes exactos; el JSON re-serializado produce otra firma.
    body = await request.body()

    if not verificar_firma(body, x_hub_signature_256, settings.wa_app_secret):
        log.warning("firma inválida; %d bytes descartados", len(body))
        return JSONResponse({"error": "firma inválida"}, status_code=403)

    try:
        payload: Any = await request.json()
    except ValueError:
        # Firma válida pero cuerpo no-JSON. No hay nada que reintentar.
        log.warning("cuerpo con firma válida pero JSON inválido")
        return JSONResponse({"estado": "ignorado"}, status_code=200)

    lote = parsear_webhook(payload)

    if not lote.mensajes:
        # Acuses de entrega y notificaciones que no disparan al agente.
        return JSONResponse(
            {"estado": "sin_mensajes", "estados": len(lote.estados)}, status_code=200
        )

    encolados = 0
    try:
        for mensaje in lote.mensajes:
            wa_id_hash = hash_wa_id(mensaje.wa_id, settings.wa_id_pepper)
            nuevo = await cola.encolar(
                wa_message_id=mensaje.wa_message_id,
                wa_id_hash=wa_id_hash,
                payload=mensaje.model_dump(mode="json"),
            )
            if nuevo:
                encolados += 1
            else:
                log.info(
                    "duplicado descartado: %s de %s",
                    mensaje.wa_message_id,
                    enmascarar(mensaje.wa_id),
                )
    except Exception:
        # Meta reintenta con backoff. Perder el mensaje sería peor.
        log.exception("fallo al encolar; se pide reintento a Meta")
        return JSONResponse({"error": "no disponible"}, status_code=503)

    return JSONResponse(
        {"estado": "ok", "encolados": encolados, "recibidos": len(lote.mensajes)},
        status_code=200,
    )
