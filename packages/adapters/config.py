"""Configuración por entorno.

Falla al arrancar si falta algo obligatorio. Un servicio que levanta con el
`WA_APP_SECRET` vacío acepta webhooks sin verificar, y eso no debe poder pasar
por descuido.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ── Transporte de WhatsApp ──
    # "meta"  → Cloud API directa. `WA_ACCESS_TOKEN` autentica el envío y
    #           `WA_APP_SECRET` verifica la firma entrante.
    # "kapso" → Kapso como proveedor. Es un proxy compatible con Meta: el
    #           cuerpo de los mensajes es idéntico, así que solo cambian la
    #           URL, la cabecera de autenticación y la de firma.
    wa_transporte: Literal["meta", "kapso"] = "meta"

    # Solo para wa_transporte="kapso".
    kapso_api_key: str = ""
    kapso_base_url: str = "https://api.kapso.ai"
    # Secreto del webhook configurado en Kapso (`--secret-key`). Firma en
    # `X-Webhook-Signature`, hex pelado, sobre el cuerpo crudo.
    kapso_webhook_secret: str = ""

    # ── WhatsApp ──
    wa_app_secret: str = Field(min_length=1)
    wa_verify_token: str = Field(min_length=1)
    wa_access_token: str = ""
    wa_phone_number_id: str = ""
    # Fijada a propósito. "latest" cambia bajo los pies sin aviso.
    wa_graph_version: str = "v23.0"

    # ── Identidad ──
    # Sin pepper el hash de un móvil chileno es reversible por fuerza bruta.
    wa_id_pepper: str = Field(min_length=32)

    # Altas declarativas que el worker aplica al arrancar (invariante 4).
    # Formato: "+56 9 6626 6451:consultar,cotizar; 56900000000:consultar".
    # Quitar a alguien de aquí NO lo revoca: revocar es explícito, para que un
    # despliegue con la variable mal copiada no corte accesos en silencio.
    clientes_autorizados: str = ""

    # Acceso abierto: cualquiera que escriba puede usar el agente sin figurar
    # en `ventupilot.clientes`.
    #
    # Es una decisión de producto, no un atajo técnico, y tiene tres costes
    # que conviene tener presentes al activarlo:
    #
    # - Cualquiera que dé con el número consume tokens y mensajes de WhatsApp.
    #   No hay tope por remitente, solo por run.
    # - Se pierde la trazabilidad de a quién se cotizó: no hay `customer_id`
    #   que enlace la propuesta con un cliente de ventu 1.0.
    # - La invariante 4 sigue en pie —el teléfono no autoriza por sí mismo—
    #   pero la política pasa a ser "todos", que es una autorización explícita
    #   y revisable, no una deducción a partir de un dato de terceros.
    #
    # No pisa una desactivación: quien esté registrado como inactivo sigue
    # fuera, porque desactivar es un acto deliberado y esto no debe anularlo.
    acceso_abierto: bool = False
    # Qué se concede en acceso abierto. `pedir` queda fuera del defecto a
    # propósito: es el permiso con impacto económico directo.
    permisos_abiertos: str = "consultar,cotizar"

    # ── Datos ──
    # Esquema propio dentro de la Postgres de ventu 1.0.
    database_url: str = Field(min_length=1)
    # Rol de solo lectura sobre el catálogo de productos. Separado a propósito:
    # es lo que hace que un prompt injection no tenga privilegio que escalar.
    database_url_ro: str = ""
    db_pool_min: int = 1
    # Techo explícito: con varias réplicas es fácil agotar max_connections, y
    # el que se cae entonces es la aplicación principal, no el agente.
    db_pool_max: int = 5

    # ── Modelo ──
    openai_api_key: str = ""
    agent_model: str = "openai:gpt-5.6-terra"

    # ── Precios (motor de ventu 1.0) ──
    # De dónde sale el precio que se cotiza:
    #
    # "motor"  → pricing_productpriceresult.precio_final, la salida del motor
    #            de precios de ventu 1.0 (costo, markup, IVA, campañas).
    # "costo"  → costo bruto del producto por `precio_factor`. Decisión de
    #            negocio: cubre todo el catálogo con costo, no solo lo que el
    #            motor alcanzó a calcular, a cambio de no aplicar campañas ni
    #            la aritmética específica de cada canal.
    #
    # En ambos casos el precio lo produce el backend, nunca el modelo
    # (invariante 1). Lo que cambia es la fórmula, no quién la ejecuta.
    precio_origen: Literal["motor", "costo"] = "motor"

    # Multiplicador sobre el costo cuando precio_origen="costo".
    precio_factor: Decimal = Decimal("1.4")

    # Qué costo se toma como base.
    # "credito" → costo_credito (D3, web LK). Es el **bruto**, y el que usa el
    #             propio motor de ventu 1.0 en marketplace_price.
    # "contado" → costo_contado (D4), que el modelo de ventu 1.0 etiqueta como
    #             NETO. Usarlo con el mismo factor da un precio sistemáticamente
    #             más bajo: no son bases intercambiables.
    precio_costo: Literal["credito", "contado"] = "credito"

    # Canal del motor que define qué precio ve el agente: global | ml | shopify.
    # Solo se usa con precio_origen="motor".
    pricing_channel: str = "global"
    # Un precio recalculado hace semanas no es un precio. Por encima de este
    # umbral el producto se considera sin precio vigente y no se ofrece.
    # Ojo: si el motor de precios de ventu 1.0 deja de correr, esto vacía el
    # catálogo ofrecible en silencio.
    precio_max_edad_horas: int = 48

    # ── Límites ──
    max_requests_per_run: int = 6
    max_tool_calls_per_run: int = 12
    propuesta_ttl_min: int = 30
    # Tope de resultados que una búsqueda devuelve al modelo. Más que esto no
    # mejora la respuesta y sí infla el costo de cada turno.
    max_resultados_busqueda: int = 8

    log_level: str = "INFO"

    @property
    def secreto_de_firma(self) -> str:
        """Secreto con el que se verifica el webhook entrante.

        Cambia con el transporte: Meta firma con el app secret, Kapso con el
        secreto propio del webhook. Resolverlo aquí evita que cada llamador
        tenga que acordarse del `if`.
        """
        if self.wa_transporte == "kapso":
            return self.kapso_webhook_secret
        return self.wa_app_secret


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


class PrisaSettings(BaseSettings):
    """Credenciales y opciones del portal de prisa.cl.

    Separada de `Settings` a propósito: aquella exige lo del agente de WhatsApp
    —`wa_app_secret`, `database_url`, el pepper— y entrar en prisa.cl no
    necesita nada de eso. Fundirlas obligaría a tener toda la configuración del
    worker montada para poder correr un login.

    Lee `.env` además del entorno, como el resto del proyecto. Eso no es solo
    consistencia: `export PRISA_PASSWORD="clave$con!signos"` deja que el shell
    se coma el `$` y el `!` y guarda otra cosa, y el síntoma —«los datos
    ingresados son incorrectos»— apunta al sitio en vez de al shell. En el
    fichero no hay expansión que valga.
    """

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # RUT con guion y dígito verificador (`12345678-9`) o correo: el formulario
    # acepta ambos.
    prisa_usuario: str = ""
    prisa_password: str = ""

    prisa_base_url: str = "https://www.prisa.cl"
    # Dónde se guardan las cookies. Valen lo mismo que la contraseña mientras
    # duran, así que el fichero se crea con permisos 0600.
    prisa_sesion_path: str = ".prisa-sesion.json"
    # Proxy de salida del navegador. Vacío = directo. Se usa si el reCAPTCHA
    # empieza a levantar retos, que depende de la reputación de la IP.
    prisa_proxy: str = ""
    # Chromium ya instalado, para imágenes que traen el suyo.
    prisa_chromium_path: str = ""


@lru_cache
def get_prisa_settings() -> PrisaSettings:
    return PrisaSettings()
