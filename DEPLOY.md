# Despliegue y conexión con Meta

El orden importa: Meta solo acepta la suscripción si el webhook **ya está vivo**
y responde el handshake. No se puede configurar primero en Meta y desplegar
después.

```
1. Migración   →  2. Servicio en Railway  →  3. Dominio  →  4. Meta
```

---

## 1. Migración

Contra la Postgres de `ventu-prod`. Crea el esquema `ventupilot` y nada fuera de él.

```bash
psql "$DATABASE_URL" -f migrations/001_inicial.sql
```

Y el rol de solo lectura para el catálogo, que es una tabla-por-tabla a propósito:

```sql
CREATE ROLE ventupilot_ro LOGIN PASSWORD '<generar>';
GRANT USAGE ON SCHEMA public TO ventupilot_ro;
GRANT SELECT ON productos, precios TO ventupilot_ro;  -- ajustar a las reales
```

No uses `GRANT SELECT ON ALL TABLES`: vuelve a abrir todo y anula el punto del rol.

## 2. Servicio en Railway

En el proyecto **ventu-prod** (`edbf8c34-650c-4334-b744-0f264102fc7e`):

1. **New Service → GitHub Repo** → `ventuglobal/ventupilot`, rama `claude/whatsapp-purchase-agent-dclrk8`.
2. Nombre: `wa-gateway`.
3. **Settings → Config-as-code**: `railway.gateway.json`.

   Si prefieres no usarlo, el start command es:
   ```
   uvicorn services.wa_gateway.main:app --host 0.0.0.0 --port $PORT
   ```
   `$PORT` lo inyecta Railway. Fijarlo a un número hace que el healthcheck falle.

4. **Variables**:

   | Variable | Valor |
   |---|---|
   | `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` — referencia al servicio, no la cadena literal |
   | `WA_APP_SECRET` | App secret de Meta → Settings → Basic |
   | `WA_VERIFY_TOKEN` | Lo inventas tú. Guárdalo: hace falta en el paso 4 |
   | `WA_ID_PEPPER` | `python -c "import secrets; print(secrets.token_hex(32))"` |

   El servicio **no arranca** si falta cualquiera de las cuatro. Es deliberado:
   un gateway con `WA_APP_SECRET` vacío aceptaría webhooks sin verificar.

5. **Settings → Resources**: pon un límite de memoria. Comparte proyecto con
   ventu 1.0 y no quieres que el gateway le compita por recursos.

### Servicio 2: agent-worker

Mismo repo y misma rama, nombre `agent-worker`, config `railway.worker.json`.
No expone puerto y no necesita dominio: solo lee la cola.

Variables: las mismas cuatro del gateway, más las de envío —

| Variable | Valor |
|---|---|
| `WA_ACCESS_TOKEN` | System User token, permanente |
| `WA_PHONE_NUMBER_ID` | Meta → WhatsApp → API Setup |

**`WA_ID_PEPPER` tiene que ser idéntico en ambos servicios.** Si difieren, cada
uno calcula un `wa_id_hash` distinto para el mismo número y las conversaciones
se parten en dos sin dar ningún error.

Empieza con **1 réplica**. El advisory lock protege el orden dentro de cada
conversación, pero no hay razón para escalar antes de tener volumen medido.

## 3. Dominio

**Settings → Networking → Generate Domain.**

Railway devuelve algo con la forma `wa-gateway-production-XXXX.up.railway.app`.
El sufijo lo asigna Railway y no es predecible: hay que leerlo del dashboard
después de generarlo. Si prefieres un dominio estable, usa **Custom Domain** con
un subdominio propio (`wa.ventu.cl`) y evitas depender del generado.

Verifica antes de tocar Meta:

```bash
curl https://<tu-dominio>/health
# {"estado":"ok"}

curl "https://<tu-dominio>/webhook?hub.mode=subscribe&hub.verify_token=<TU_TOKEN>&hub.challenge=12345"
# 12345      ← texto plano, sin comillas
```

Si el segundo devuelve `403`, el `WA_VERIFY_TOKEN` del servicio no coincide con
el que estás pasando. Meta no da más detalle que "no se pudo validar", así que
conviene descartarlo con curl primero.

## 4. Meta

**App Dashboard → WhatsApp → Configuration → Webhook → Edit:**

| Campo | Valor |
|---|---|
| **Callback URL** | `https://<tu-dominio>/webhook` |
| **Verify Token** | el mismo `WA_VERIFY_TOKEN` del servicio |

Meta llama al `GET` en ese momento. Si el token coincide, la suscripción queda
verificada.

Después, en **Webhook fields**, suscribe `messages`. Sin esa suscripción el
webhook queda verificado pero no llega nada — es el error más común después de
configurar la URL, porque la pantalla no avisa de que falta.

## Verificación de extremo a extremo

Manda un WhatsApp al número desde tu celular y revisa los logs:

```
INFO wa_gateway: ... encolados=1
```

Manda **el mismo mensaje dos veces**. El segundo debe aparecer como duplicado
descartado y `encolados=0`: es la prueba de que el dedupe funciona y de que un
reintento de Meta no correrá el agente dos veces.

## Notas

- **La firma se calcula sobre el cuerpo crudo.** Si alguna vez metes un proxy o
  middleware que reescriba el body, la verificación empieza a fallar con 403 y
  el motivo no es obvio.
- **Códigos de respuesta**: 403 firma inválida (Meta no reintenta), 200 payload
  sin mensajes, 503 si falla la base — ahí sí queremos el reintento, porque un
  200 con la base caída descarta el mensaje de un cliente en silencio.
- **El worker responde con un eco.** Es deliberado: la Fase 1 prueba el
  transporte completo sin nada de IA de por medio. El agente entra en Fase 2
  sustituyendo un handler, sin tocar el resto.
- **Audio e imágenes se rechazan con un mensaje explícito**, no con silencio. El
  cliente que manda una nota de voz recibe una respuesta que le dice qué hacer.
