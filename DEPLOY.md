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

En el proyecto **ventu-prod** (`36f9f6cb-c38a-47d0-ab2d-59c348af9a38`):

> Los dos servicios **ya están creados** en ese proyecto, con su
> config-as-code, sus variables no-Meta y el dominio del gateway. Esta sección
> queda como referencia de qué es cada cosa y para recrearlos desde cero.
>
> | Servicio | ID |
> |---|---|
> | `wa-gateway` | `4abfa9c7-c55a-4432-9db7-19bbd6908d61` |
> | `agent-worker` | `17798411-5e88-4cd4-8c56-9a7ffce98006` |
>
> Son servicios **propios**: no comparten cómputo, despliegue ni escalado con
> `web` y `worker` de ventu 1.0. Lo único compartido es la Postgres, y ahí
> ventupilot escribe solo en su esquema.

1. **New Service → GitHub Repo** → `ventuglobal/ventupilot`.
2. Nombre: `wa-gateway`.
3. **Settings → Source → Branch**: la rama que quieras desplegar. Un *project
   token* no puede cambiarla por API (`serviceConnect` responde `Not
   Authorized`), así que este paso es de panel sí o sí.
4. **Settings → Config-as-code**: `railway.gateway.json`.

   Si prefieres no usarlo, el start command es:
   ```
   uvicorn services.wa_gateway.main:app --host 0.0.0.0 --port $PORT
   ```
   `$PORT` lo inyecta Railway. Fijarlo a un número hace que el healthcheck falle.

5. **Variables**:

   | Variable | Valor |
   |---|---|
   | `DATABASE_URL` | `${{Postgres.DATABASE_URL}}` — referencia al servicio, no la cadena literal |
   | `WA_APP_SECRET` | App secret de Meta → Settings → Basic |
   | `WA_VERIFY_TOKEN` | Lo inventas tú. Guárdalo: hace falta en el paso 4 |
   | `WA_ID_PEPPER` | `python -c "import secrets; print(secrets.token_hex(32))"` |

   El servicio **no arranca** si falta cualquiera de las cuatro. Es deliberado:
   un gateway con `WA_APP_SECRET` vacío aceptaría webhooks sin verificar.

6. **Settings → Resources**: pon un límite de memoria. Comparte proyecto con
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

Ya generado:

```
https://wa-gateway-production-7cee.up.railway.app
```

El sufijo lo asignó Railway. Si prefieres algo estable y legible, añade un
**Custom Domain** (`wa.ventu.cl`) y usa ese en Meta: cambiar la Callback URL más
tarde obliga a re-verificar el webhook.

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

## 4. Proveedor: Meta directo o Kapso

`WA_TRANSPORTE` decide cuál. Kapso es un **proxy compatible con Meta**: mismo
cuerpo JSON en los mensajes, y con `--kind meta` reenvía el payload entrante sin
modificar. Por eso cambiar de proveedor no toca el agente ni el render de las
cotizaciones — solo la URL de envío, la cabecera de autenticación y la de firma.

### Opción A — Kapso

```bash
npm install -g @kapso/cli
kapso login                 # OAuth por navegador
kapso setup                 # provisiona el número
```

Después, apuntar el webhook al gateway ya desplegado:

```bash
kapso whatsapp webhooks new \
  --url https://wa-gateway-production-7cee.up.railway.app/webhook \
  --kind meta \
  --event whatsapp.message.received \
  --secret-key "<generar y guardar>" \
  --active
```

`--kind meta` es obligatorio para que el parser siga sirviendo. El
`--secret-key` va a `KAPSO_WEBHOOK_SECRET` en los dos servicios de Railway, y
`WA_TRANSPORTE=kapso` con `KAPSO_API_KEY`.

Dos diferencias con Meta que muerden si no se ven venir:

- **La firma llega en `X-Webhook-Signature`, en hex pelado**, sin el prefijo
  `sha256=`. Es HMAC-SHA256 sobre el cuerpo crudo, igual que Meta.
- **No hay handshake GET.** Kapso no verifica la URL como hace Meta, así que
  `WA_VERIFY_TOKEN` deja de usarse en este modo.

Kapso además ofrece buffering (`--buffer-enabled`, `--buffer-window-seconds`),
que agrupa mensajes seguidos del mismo remitente. Se solapa con el advisory lock
por conversación: no hace falta, pero puede reducir turnos —y por tanto costo—
si se activa con una ventana corta.

### Opción B — Meta directo

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
- **Qué versión se despliega depende de la rama del servicio.** La rama por
  defecto del repo trae el handler de eco (Fase 1): transporte completo sin IA,
  útil para diagnosticar. El agente (Fase 2) está en
  `claude/railway-ventu-prod-connect-bhtavh` hasta que se mergee. Si el número
  contesta "recibí: ..." en vez de conversar, es que el servicio está en la rama
  de Fase 1.
- **No interferir con ventu 1.0.** `wa-gateway` y `agent-worker` son servicios
  aparte y no tocan `web` ni `worker`. Lo único compartido es la Postgres:
  ventupilot escribe solo en el esquema `ventupilot` y lee `public` con el rol
  de solo lectura. Por eso el pool tiene techo explícito — agotar
  `max_connections` tumbaría ventu 1.0, no al agente.
- **Audio e imágenes se rechazan con un mensaje explícito**, no con silencio. El
  cliente que manda una nota de voz recibe una respuesta que le dice qué hacer.
