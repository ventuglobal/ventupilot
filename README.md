# ventupilot

Agente conversacional de compra de productos por WhatsApp.

## Estado

Fase 1 en curso. Lo que existe hoy es la capa de transporte: verificación de
firma, hasheo de identidad y parseo del webhook, todo con tests y sin
dependencias externas. El gateway HTTP, la cola y el agente vienen después.

## Arquitectura

```
Meta Cloud API ──▶ wa-gateway ──▶ cola (Postgres) ──▶ agent-worker ──▶ Saleor GraphQL
   (webhook)       verifica HMAC                       Pydantic AI      (HTTPS público)
                   dedupe                              tools
                   200 en <2s                          responde
```

Dos servicios de Railway en el proyecto `ventupilot`, más una Postgres propia.
**Saleor vive en otro proyecto de Railway** (`ventu-saleor`), y la red privada
de Railway existe solo dentro de un mismo proyecto y entorno — así que Saleor se
alcanza por su endpoint GraphQL público con un App token de permisos mínimos.

Eso tiene una consecuencia de diseño que conviene entender antes de tocar nada:
**la base de datos de Saleor no es la fuente de precios.** Los precios de Saleor
dependen del canal, de las listas de precios por canal, de las promociones
activas y de la configuración de impuestos. Reconstruir eso con `SELECT` sobre
sus tablas es reimplementar su motor de precios, y va a divergir. Todo precio
sale de la GraphQL API.

## Invariantes

Se revisan en cada PR. Si una se rompe, el PR no entra.

1. **El modelo nunca calcula dinero.** Totales, descuentos, impuestos y plazos
   salen de Saleor. El agente devuelve SKUs y cantidades; el backend consulta
   los precios y renderiza el mensaje.
2. **El modelo nunca ejecuta la transacción.** Propone; el usuario confirma con
   un elemento estructurado; el backend ejecuta.
3. **La identidad viene de `deps`, no de argumentos del modelo.**
4. **El número de teléfono es identidad, no autorización.** Toda operación con
   impacto económico verifica permisos contra la base de datos.
5. **Toda escritura es idempotente**, con clave determinística.
6. **El webhook responde 200 en menos de 2s**, pase lo que pase aguas abajo.
7. **Cada run tiene límite de requests y de tool calls.**
8. **Nada de PII en logs.** Teléfonos hasheados con pepper, cuerpos de mensaje
   fuera del log de aplicación.

## Desarrollo

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run mypy packages services
```

Copia `.env.example` a `.env` y complétalo. `WA_ID_PEPPER` se genera con
`python -c "import secrets; print(secrets.token_hex(32))"` y rotarlo invalida
todos los `wa_id_hash` almacenados.

## Estructura

```
packages/
  domain/            modelos de negocio, sin I/O
  adapters/          I/O: Saleor, Postgres, WhatsApp
    whatsapp/
      signature.py   HMAC del webhook
      identity.py    hasheo de wa_id
      payloads.py    parseo del payload anidado
  agents/            agente y tools (Pydantic AI)
services/
  wa_gateway/        FastAPI: recibe, verifica, encola
  agent_worker/      consume la cola, corre el agente, responde
```

`domain/` no importa nada de `adapters/`. Esa frontera es lo que permite testear
la lógica sin levantar Postgres.

## Notas de operación

- **`WA_GRAPH_VERSION` se fija**, nunca `latest`: Meta introduce cambios
  incompatibles entre versiones.
- **El token de desarrollador de Meta expira en 24h.** Si la integración deja de
  funcionar al día siguiente, es eso: hace falta un System User token.
- **La firma se calcula sobre el cuerpo crudo**, no sobre el JSON re-serializado.
  Hay un test que se pone rojo si alguien lo cambia.
- **Costo por conversación**: con `gpt-5.6-terra`, unos $0.10–0.15 en tokens por
  conversación de ~8 turnos, más un orden similar en mensajes de WhatsApp en
  Chile una vez que el cobro de servicio y utility dentro de la ventana de 24h
  entre en vigor el 1-oct-2026. Cada turno que se ahorra cuenta en ambas columnas.
