"""Tests del gateway.

La cola se sustituye por un doble en memoria: lo que se verifica aquí es el
contrato HTTP con Meta —qué se acepta, qué se rechaza y con qué código— no la
persistencia.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from packages.adapters.config import get_settings
from packages.adapters.whatsapp.signature import firmar

SECRET = "secret-de-prueba"
VERIFY_TOKEN = "token-de-verificacion"
PEPPER = "p" * 64


class ColaFalsa:
    """Doble de `ColaRepo` que recuerda lo encolado y sabe fallar a voluntad."""

    def __init__(self) -> None:
        self.encolados: list[str] = []
        self.vistos: set[str] = set()
        self.explota = False

    async def encolar(
        self, wa_message_id: str, wa_id_hash: str, payload: dict[str, Any]
    ) -> bool:
        if self.explota:
            raise RuntimeError("base de datos caída")
        if wa_message_id in self.vistos:
            return False
        self.vistos.add(wa_message_id)
        self.encolados.append(wa_message_id)
        return True


@pytest.fixture
def cola() -> ColaFalsa:
    return ColaFalsa()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, cola: ColaFalsa) -> TestClient:
    for clave, valor in {
        "WA_APP_SECRET": SECRET,
        "WA_VERIFY_TOKEN": VERIFY_TOKEN,
        "WA_ID_PEPPER": PEPPER,
        "DATABASE_URL": "postgresql://noop/noop",
    }.items():
        monkeypatch.setenv(clave, valor)
    get_settings.cache_clear()

    from services.wa_gateway import main as gateway

    class PoolFalso:
        async def close(self) -> None:
            return None

    async def _pool_falso(*_: object, **__: object) -> PoolFalso:
        return PoolFalso()

    monkeypatch.setattr(gateway.asyncpg, "create_pool", _pool_falso)
    monkeypatch.setattr(gateway, "ColaRepo", lambda _pool: cola)

    with TestClient(gateway.app) as c:
        yield c

    get_settings.cache_clear()


def _post(client: TestClient, payload: dict[str, Any], secret: str = SECRET):
    # Bytes exactos, firmados y enviados. Reutilizar el dict y dejar que httpx
    # lo re-serialice rompería la firma, que es justo lo que queremos evitar.
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return client.post(
        "/webhook",
        content=body,
        headers={
            "X-Hub-Signature-256": firmar(body, secret),
            "Content-Type": "application/json",
        },
    )


def _mensaje_texto(wa_message_id: str, texto: str = "hola") -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": "PNID"},
                            "contacts": [
                                {"profile": {"name": "Juan"}, "wa_id": "56912345678"}
                            ],
                            "messages": [
                                {
                                    "from": "56912345678",
                                    "id": wa_message_id,
                                    "timestamp": "1755260000",
                                    "type": "text",
                                    "text": {"body": texto},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


class TestHandshake:
    def test_token_correcto_devuelve_el_challenge_en_texto_plano(
        self, client: TestClient
    ) -> None:
        r = client.get(
            "/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": VERIFY_TOKEN,
                "hub.challenge": "1158201444",
            },
        )
        assert r.status_code == 200
        # Sin comillas y sin JSON: Meta rechaza la suscripción si viene envuelto.
        assert r.text == "1158201444"
        assert r.headers["content-type"].startswith("text/plain")

    def test_token_incorrecto_da_403(self, client: TestClient) -> None:
        r = client.get(
            "/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "equivocado",
                "hub.challenge": "1158201444",
            },
        )
        assert r.status_code == 403


class TestRecepcion:
    def test_mensaje_valido_se_encola(self, client: TestClient, cola: ColaFalsa) -> None:
        r = _post(client, _mensaje_texto("wamid.A"))
        assert r.status_code == 200
        assert r.json()["encolados"] == 1
        assert cola.encolados == ["wamid.A"]

    def test_firma_invalida_da_403_y_no_encola(
        self, client: TestClient, cola: ColaFalsa
    ) -> None:
        r = _post(client, _mensaje_texto("wamid.B"), secret="secret-equivocado")
        assert r.status_code == 403
        assert cola.encolados == []

    def test_sin_cabecera_de_firma_da_403(
        self, client: TestClient, cola: ColaFalsa
    ) -> None:
        r = client.post("/webhook", json=_mensaje_texto("wamid.C"))
        assert r.status_code == 403
        assert cola.encolados == []

    def test_el_mismo_mensaje_dos_veces_se_encola_una_sola(
        self, client: TestClient, cola: ColaFalsa
    ) -> None:
        """Meta reintenta. Sin dedupe, el agente correría dos veces."""
        payload = _mensaje_texto("wamid.D")

        primera = _post(client, payload)
        segunda = _post(client, payload)

        assert primera.json()["encolados"] == 1
        assert segunda.json()["encolados"] == 0
        assert segunda.status_code == 200
        assert cola.encolados == ["wamid.D"]

    def test_acuses_de_entrega_no_encolan_nada(
        self, client: TestClient, cola: ColaFalsa
    ) -> None:
        payload = {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "metadata": {"phone_number_id": "PNID"},
                                "statuses": [
                                    {
                                        "id": "wamid.OUT",
                                        "status": "delivered",
                                        "recipient_id": "56912345678",
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        }
        r = _post(client, payload)
        assert r.status_code == 200
        assert r.json()["estado"] == "sin_mensajes"
        assert cola.encolados == []

    def test_fallo_de_base_pide_reintento_en_vez_de_perder_el_mensaje(
        self, client: TestClient, cola: ColaFalsa
    ) -> None:
        """503, no 200.

        Responder 200 con la base caída descarta el mensaje de un cliente en
        silencio. El reintento de Meta cuesta latencia; el 200 cuesta la venta.
        """
        cola.explota = True
        r = _post(client, _mensaje_texto("wamid.E"))
        assert r.status_code == 503

    def test_json_invalido_con_firma_valida_no_pide_reintento(
        self, client: TestClient
    ) -> None:
        body = b"no soy json"
        r = client.post(
            "/webhook",
            content=body,
            headers={
                "X-Hub-Signature-256": firmar(body, SECRET),
                "Content-Type": "application/json",
            },
        )
        assert r.status_code == 200

    def test_health_responde(self, client: TestClient) -> None:
        assert client.get("/health").json() == {"estado": "ok"}
