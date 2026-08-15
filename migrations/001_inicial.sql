-- ventupilot — esquema inicial
--
-- Vive en un esquema propio dentro de la Postgres de ventu 1.0. Todo lo que
-- crea este archivo está bajo `ventupilot.`; nada toca las tablas de la
-- aplicación principal.
--
-- Aplicar:  psql "$DATABASE_URL" -f migrations/001_inicial.sql

BEGIN;

CREATE SCHEMA IF NOT EXISTS ventupilot;

-- ─── Dedupe del webhook ──────────────────────────────────────────────────────
-- Meta reintenta ante cualquier respuesta que no sea 2xx, y a veces entrega el
-- mismo mensaje dos veces aunque hayamos respondido bien. Sin esta tabla, un
-- reintento vuelve a correr el agente y puede duplicar una propuesta.
CREATE TABLE IF NOT EXISTS ventupilot.mensajes_procesados (
    wa_message_id  TEXT PRIMARY KEY,
    recibido_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Los reintentos de Meta ocurren en minutos, no en días. Purgar por debajo de
-- este índice mantiene la tabla pequeña sin perder la protección.
CREATE INDEX IF NOT EXISTS ix_mensajes_procesados_recibido
    ON ventupilot.mensajes_procesados (recibido_at);

-- ─── Cola de trabajo ─────────────────────────────────────────────────────────
-- Postgres en vez de Redis: la instancia ya existe, y el lock por conversación
-- (advisory lock) tiene que vivir aquí de todas formas. Un servicio menos.
-- CREATE TYPE no admite IF NOT EXISTS: el bloque lo hace reejecutable.
DO $$
BEGIN
    CREATE TYPE ventupilot.estado_tarea AS ENUM (
        'pendiente', 'en_proceso', 'completada', 'fallida'
    );
EXCEPTION
    WHEN duplicate_object THEN NULL;
END
$$;

CREATE TABLE IF NOT EXISTS ventupilot.cola (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    wa_message_id    TEXT NOT NULL REFERENCES ventupilot.mensajes_procesados (wa_message_id),
    conversacion_id  BIGINT,
    -- Clave de particionado del lock: garantiza que dos mensajes del mismo
    -- remitente nunca se procesen en paralelo.
    lock_key         BIGINT NOT NULL,
    payload          JSONB NOT NULL,
    estado           ventupilot.estado_tarea NOT NULL DEFAULT 'pendiente',
    intentos         SMALLINT NOT NULL DEFAULT 0,
    ultimo_error     TEXT,
    disponible_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    creada_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    actualizada_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_cola_pendientes
    ON ventupilot.cola (disponible_at)
    WHERE estado = 'pendiente';

-- ─── Conversación ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ventupilot.conversaciones (
    id                     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    wa_id_hash             TEXT NOT NULL,
    phone_number_id        TEXT NOT NULL,
    agente                 TEXT NOT NULL DEFAULT 'compra',
    estado                 TEXT NOT NULL DEFAULT 'activa',
    -- Sin este campo, cada envío fuera de ventana es una apuesta: determina si
    -- podemos mandar un mensaje libre o hace falta una plantilla.
    ultimo_msg_usuario_at  TIMESTAMPTZ,
    creada_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    actualizada_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_conversacion_activa
    ON ventupilot.conversaciones (wa_id_hash, phone_number_id)
    WHERE estado = 'activa';

CREATE TABLE IF NOT EXISTS ventupilot.historial (
    conversacion_id  BIGINT NOT NULL REFERENCES ventupilot.conversaciones (id) ON DELETE CASCADE,
    seq              INTEGER NOT NULL,
    payload          JSONB NOT NULL,
    creado_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (conversacion_id, seq)
);

-- ─── Outbox de salida ────────────────────────────────────────────────────────
-- Si la tool ya se ejecutó y el POST a Graph API falla, el estado en la base y
-- lo que el cliente sabe quedan desincronizados. El outbox hace que el envío
-- sea reintentable igual que la entrada es idempotente.
CREATE TABLE IF NOT EXISTS ventupilot.mensajes_salientes (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    conversacion_id  BIGINT REFERENCES ventupilot.conversaciones (id) ON DELETE SET NULL,
    wa_id_hash       TEXT NOT NULL,
    -- Determinística: reintentar no duplica el mensaje para el usuario.
    idempotency_key  TEXT NOT NULL UNIQUE,
    cuerpo           JSONB NOT NULL,
    estado           TEXT NOT NULL DEFAULT 'pendiente',
    intentos         SMALLINT NOT NULL DEFAULT 0,
    wa_message_id    TEXT,
    ultimo_error     TEXT,
    disponible_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    creada_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_salientes_pendientes
    ON ventupilot.mensajes_salientes (disponible_at)
    WHERE estado = 'pendiente';

-- ─── Transaccional ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ventupilot.propuestas (
    id               TEXT PRIMARY KEY,
    conversacion_id  BIGINT NOT NULL REFERENCES ventupilot.conversaciones (id),
    wa_id_hash       TEXT NOT NULL,
    -- Congela los precios al momento de cotizar. Si cambian entre la propuesta
    -- y la confirmación, decide el sistema comparando contra esto — no el modelo.
    snapshot         JSONB NOT NULL,
    total            NUMERIC(14, 2) NOT NULL,
    moneda           CHAR(3) NOT NULL DEFAULT 'CLP',
    estado           TEXT NOT NULL DEFAULT 'vigente',
    expira_at        TIMESTAMPTZ NOT NULL,
    creada_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_propuestas_vigentes
    ON ventupilot.propuestas (conversacion_id, expira_at)
    WHERE estado = 'vigente';

CREATE TABLE IF NOT EXISTS ventupilot.ordenes (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    propuesta_id     TEXT NOT NULL REFERENCES ventupilot.propuestas (id),
    -- Última defensa contra duplicados. En la base, no solo en el código:
    -- confirmar dos veces la misma propuesta debe crear una sola orden.
    idempotency_key  TEXT NOT NULL UNIQUE,
    referencia_ext   TEXT,
    estado           TEXT NOT NULL DEFAULT 'creada',
    creada_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ─── Observabilidad de negocio ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ventupilot.eventos_agente (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    conversacion_id  BIGINT REFERENCES ventupilot.conversaciones (id) ON DELETE CASCADE,
    run_id           TEXT,
    tipo             TEXT NOT NULL,
    payload          JSONB NOT NULL DEFAULT '{}'::jsonb,
    creado_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_eventos_conversacion
    ON ventupilot.eventos_agente (conversacion_id, creado_at);

COMMIT;
