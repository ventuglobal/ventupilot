# ventupilot

Agente conversacional de compra de productos por WhatsApp.

## Estado

**Fase 2 en curso.** El circuito de la Fase 1 sigue igual —gateway, cola,
outbox— y el handler de eco quedó sustituido por el agente Pydantic AI, con su
tool de catálogo sobre los datos de ventu 1.0, historial por conversación y
cotizaciones confirmables por botón.

Pendiente: desplegar, conectar con Meta (ver [DEPLOY.md](DEPLOY.md)) y dar de
alta remitentes — ver [Pendiente](#pendiente).

## Arquitectura

```
Meta Cloud API ──▶ wa-gateway ──▶ cola (Postgres) ──▶ agent-worker ──▶ ventu 1.0
   (webhook)       verifica HMAC   esquema             Pydantic AI      catálogo y
                   dedupe          ventupilot          tools            precios
                   200 en <2s                          outbox           (rol RO,
                                                                        red privada)
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

1. **El modelo nunca calcula dinero.** El agente devuelve SKUs y cantidades
   —`SeleccionLinea`, un tipo sin dónde poner un monto— y el backend valoriza
   y renderiza. La invariante la garantiza el código, no una instrucción del
   prompt. La fórmula la elige `PRECIO_ORIGEN`: el motor de precios de
   ventu 1.0, o el costo bruto por un factor. Lo que no cambia nunca es quién
   la ejecuta.
2. **El modelo nunca ejecuta la transacción.** Propone; el usuario confirma con
   un elemento estructurado; el backend ejecuta.
3. **La identidad viene de `deps`, no de argumentos del modelo.**
4. **El número de teléfono es identidad, no autorización.** Los permisos viven
   en `ventupilot.clientes` y se conceden a propósito. No se deducen de
   `orders_customer.phone` de ventu 1.0: esa columna no es única, no tiene
   índice y se llena con datos importados de MercadoLibre y Shopify.
   Autorizar por coincidencia ahí convertiría un dato de terceros en una
   credencial.
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
    outbox.py        mensajes salientes, reintentables
    catalogo.py      catálogo y precios de ventu 1.0 (rol RO, solo lectura)
    clientes.py      permisos por wa_id_hash
    conversaciones.py  conversación e historial
    propuestas.py    cotizaciones emitidas
    whatsapp/
      signature.py   HMAC del webhook
      identity.py    hasheo de wa_id con pepper
      payloads.py    parseo del payload anidado
      client.py      Graph API — el único sitio que hace POST a Meta
  agents/
    agente.py        agente Pydantic AI, tools e instrucciones
    deps.py          dependencias tipadas del run
    handler.py       enchufa el agente al worker
    mensajes.py      render de la cotización y sus botones
services/
  wa_gateway/        FastAPI: recibe, verifica, encola
  agent_worker/      consume la cola, procesa, despacha el outbox
```

`domain/` no importa nada de `adapters/`. Esa frontera es lo que permite testear
la lógica sin levantar Postgres.

## De dónde salen los datos

El agente lee dos tablas de ventu 1.0, ambas en solo lectura:

- `base_productbase` — catálogo. Se filtran los inactivos, los reabsorbidos
  (`merged_into_id`) y los que no tienen stock.
- `pricing_productpriceresult` — precio por producto y canal.

### De dónde sale el precio

`PRECIO_ORIGEN` decide entre dos fórmulas, y la diferencia es de negocio:

- **`motor`** — `pricing_productpriceresult.precio_final`, que ya resolvió
  costo, markup, IVA, campañas y la aritmética de cada canal. Es el precio con
  el que ventu 1.0 publica. Cubre solo lo que el motor alcanzó a calcular, y
  cada canal se recalcula a su propio ritmo.
- **`costo`** — `costo_credito` (el bruto, D3) por `PRECIO_FACTOR`. Cubre todo
  el catálogo que tenga costo, a cambio de no aplicar campañas ni las
  comisiones específicas de cada canal.

`costo_contado` (D4) está etiquetado **neto** en ventu 1.0. Con el mismo factor
da un precio sistemáticamente más bajo: no son bases intercambiables, y por eso
no hay fallback de una a otra.

## Dar de alta remitentes

`ventupilot.clientes` arranca vacía y nadie se auto-registra (invariante 4).
Las altas se declaran en `CLIENTES_AUTORIZADOS` y el worker las aplica al
arrancar:

```
CLIENTES_AUTORIZADOS="+56 9 6626 6451:consultar,cotizar"
```

### Acceso abierto

Para pruebas o para un canal público, `ACCESO_ABIERTO=true` concede
`PERMISOS_ABIERTOS` (por defecto `consultar,cotizar`) a cualquiera que escriba,
sin necesidad de darlo de alta.

No pisa una desactivación: quien esté registrado como inactivo sigue fuera.
Y `pedir` queda fuera del defecto porque es el permiso con impacto económico.

Con el canal abierto, la invariante 4 sigue en pie —el teléfono no autoriza por
sí mismo— pero la política pasa a ser "todos". Eso es una autorización
explícita y revisable; lo que la invariante prohíbe es deducir el permiso de un
dato de terceros, no conceder acceso general a propósito.

Declarativa y no imperativa a propósito: el estado deseado queda a la vista en
el panel, no escondido en un comando que alguien corrió una vez. Es idempotente
—hace UPSERT por `wa_id_hash`— y **no revoca**: quitar a alguien de la variable
no le corta el acceso, para que un despliegue con la variable mal copiada no
deje a nadie fuera en silencio. Revocar es explícito, con
`ClientesRepo.desactivar`.

## Ejecutar el pedido

Al confirmar una propuesta se crea, en la misma transacción, la orden real en
ventu 1.0: `orders_order` + `orders_orderitem`, con `source="whatsapp"`, junto
al registro en `ventupilot.ordenes` que enlaza ambas por `referencia_ext`.
`packages/adapters/ordenes.py` escribe las dos tablas por SQL directo —
comparten la misma Postgres— y no por un endpoint de ventu 1.0.

Apagado por defecto (`CREAR_ORDEN_VENTU=false`): escribe sobre tablas de las
que depende la facturación, y conviene encenderlo a propósito. Con el flag en
false, el flujo es el de siempre — el ejecutivo tipea el carrito a mano.

Lo que la orden **no** trae porque el botón de confirmar no lo sabe: RUT y
dirección de despacho. Sin RUT la orden no queda `is_ready_for_invoicing` en
ventu 1.0 (esa propiedad lo exige), así que sigue siendo el ejecutivo quien
contacta al cliente y completa esos datos antes de facturar — la orden ya
tiene el carrito cargado, no reemplaza esa conversación.

`orders_order` tiene ~19 columnas NOT NULL sin default a nivel de base de
datos (Django no empuja `default=` al esquema); el INSERT las lista todas
explícitamente para no depender de si la migración de ventu que agrega
`source="whatsapp"` a `Order.SOURCE_CHOICES` ya se desplegó — `choices` no es
una restricción de la base de datos, así que no importa el orden. Una columna
NOT NULL nueva que ventu agregue a `Order`/`OrderItem` y no esté en ese INSERT
rompe la creación de la orden; es el costo aceptado de SQL directo en vez de
un endpoint propio.

## Pendiente

- **Ventana de 24h.** `ultimo_msg_usuario_at` se mantiene al día, pero nadie
  decide todavía entre mensaje libre y plantilla a partir de él.
- **Métricas de entrega.** Los `statuses` del webhook se parsean y se descartan.

## Notas de operación

- **`WA_GRAPH_VERSION` se fija**, nunca `latest`: Meta introduce cambios
  incompatibles entre versiones.
- **El token de desarrollador de Meta expira en 24h.** Si la integración deja de
  funcionar al día siguiente, es eso: hace falta un System User token.
- **La firma se calcula sobre el cuerpo crudo**, no sobre el JSON re-serializado.
  Hay un test que se pone rojo si alguien lo cambia.
- **`PRECIO_MAX_EDAD_HORAS` puede vaciar el catálogo.** Si el motor de precios
  de ventu 1.0 deja de correr, los precios envejecen y el agente empieza a
  decir que no hay stock de nada. Sin error y sin alerta: es el fallo
  silencioso más probable de esta integración.
- **Costo por conversación**: con `gpt-5.6-terra`, unos $0.10–0.15 en tokens por
  conversación de ~8 turnos, más un orden similar en mensajes de WhatsApp en
  Chile una vez que el cobro de servicio y utility dentro de la ventana de 24h
  entre en vigor el 1-oct-2026. Cada turno que se ahorra cuenta en ambas columnas.
