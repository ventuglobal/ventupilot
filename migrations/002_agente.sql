-- ventupilot — Fase 2: permisos del agente
--
-- Aplicar:  psql "$DATABASE_URL" -f migrations/002_agente.sql

BEGIN;

-- ─── Remitentes autorizados ──────────────────────────────────────────────────
-- Invariante 4: el teléfono es identidad, no autorización.
--
-- Esta tabla existe porque `orders_customer` de ventu 1.0 no sirve para
-- autorizar. Es un punto de entrega deduplicado por RUT y dirección; su columna
-- `phone` no es única, no tiene índice y se llena con texto importado de
-- MercadoLibre y Shopify. Conceder permisos por coincidencia de teléfono ahí
-- convertiría un dato de terceros en una credencial.
--
-- Se puebla a propósito. Arranca vacía, y mientras lo esté ningún remitente
-- puede hacer nada: es el lado seguro por defecto.
CREATE TABLE IF NOT EXISTS ventupilot.clientes (
    wa_id_hash      TEXT PRIMARY KEY,
    -- FK lógica a orders_customer.id de ventu 1.0, para saber a quién factura.
    -- Sin REFERENCES a propósito: no queremos que un borrado en una tabla de
    -- Django falle por una dependencia nuestra, ni acoplarnos a su ciclo de
    -- vida. Es nullable porque alguien puede estar autorizado a consultar el
    -- catálogo antes de existir como cliente facturable.
    customer_id     INTEGER,
    nombre          TEXT,
    -- consultar | cotizar | pedir. Separados porque el salto de cotizar a
    -- comprometer un pedido es donde aparece el impacto económico, y conceder
    -- lo primero no debe implicar lo segundo.
    permisos        TEXT[] NOT NULL DEFAULT '{}',
    -- Corta el acceso sin perder qué tenía concedido, para poder reactivar.
    activo          BOOLEAN NOT NULL DEFAULT true,
    creado_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    actualizado_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Para el informe de "a quién tenemos habilitado", que es la consulta que hará
-- el operador. Parcial: los inactivos no interesan en ese listado.
CREATE INDEX IF NOT EXISTS ix_clientes_activos
    ON ventupilot.clientes (customer_id)
    WHERE activo;

COMMIT;
