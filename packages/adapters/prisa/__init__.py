"""Acceso autenticado a prisa.cl.

`sesion` entra con navegador (el reCAPTCHA no deja otra) y `cliente` navega
después con httpx usando las cookies que dejó. `desafio` resuelve el reto del
WAF, que aparece en ambos caminos.
"""

from __future__ import annotations

from .cliente import ClientePrisa, SesionCaducadaPrisa
from .desafio import Desafio, es_desafio, resolver
from .sesion import (
    BASE_URL,
    Credenciales,
    ErrorLoginPrisa,
    Sesion,
    iniciar_sesion,
    iniciar_sesion_manual,
)

__all__ = [
    "BASE_URL",
    "ClientePrisa",
    "Credenciales",
    "Desafio",
    "ErrorLoginPrisa",
    "Sesion",
    "SesionCaducadaPrisa",
    "es_desafio",
    "iniciar_sesion",
    "iniciar_sesion_manual",
    "resolver",
]
