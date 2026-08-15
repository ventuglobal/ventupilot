# ventupilot

Agente conversacional de compra de productos por WhatsApp.

## Estado

Fase 1. El gateway está completo: recibe, verifica firma, deduplica y encola.
El worker y el agente vienen a continuación. Para desplegarlo y conectarlo con
Meta, ver [DEPLOY.md](DEPLOY.md).

## Arquitectura

```
Meta Cloud API ──▶ wa-gateway ──▶ cola (Postgres) ──▶ agent-worker ──▶ BD productos
   (webhook)       verifica HMAC   esquema             Pydantic AI      (rol RO,
                   dedupe          ventupilot          tools             red privada)
                   200 en <2s                          responde
```

Todo vive en el proyecto **ventu-prod** de Railway, junto a ventu 1.0. Eso da
acceso por red privada a la Postgres de productos, y a cambio obliga a acotar
explícitamente lo que el agente puede hacer ahí:

- **Rol de Postgres de solo lectura** para el catálogo, con `GRANT SELECT` tabla
  por tabla. Es lo que convierte un prompt injection en un no-evento: aunque el
  modelo sea manipulado, no hay privilegio que escalar.
- **El estado del agente vive en el esquema `ventupilot`**, separado de las
  tablas de la aplicación. Se puede borrar entero sin tocar nada de ventu 1.0.
- **Techo de conexiones explícito** en el pool. Con varias réplicas es fácil
  agotar `max_connections`, y el que se cae entonces es la app principal.

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
migrations/          esquema SQL
packages/
  domain/            modelos de negocio, sin I/O
  adapters/
    config.py        settings; falla al arrancar si falta algo crítico
    cola.py          cola y dedupe sobre Postgres, con lock por conversación
    whatsapp/
      signature.py   HMAC del webhook
      identity.py    hasheo de wa_id con pepper
      payloads.py    parseo del payload anidado
  agents/            agente y tools (Pydantic AI)   ← pendiente
services/
  wa_gateway/        FastAPI: recibe, verifica, encola
  agent_worker/      consume la cola, corre el agente, responde  ← pendiente
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
