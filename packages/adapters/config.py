"""Configuración por entorno.

Falla al arrancar si falta algo obligatorio. Un servicio que levanta con el
`WA_APP_SECRET` vacío acepta webhooks sin verificar, y eso no debe poder pasar
por descuido.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

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

    # ── Límites ──
    max_requests_per_run: int = 6
    max_tool_calls_per_run: int = 12
    propuesta_ttl_min: int = 30

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
