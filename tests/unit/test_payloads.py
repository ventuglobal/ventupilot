"""Tests del parser del webhook.

Los payloads siguen la forma real de la Cloud API, no una versión idealizada:
tres niveles de anidación, `contacts` separado de `messages`, y timestamps como
strings de segundos epoch.
"""

from __future__ import annotations

from typing import Any

from packages.adapters.whatsapp.payloads import TipoMensaje, parsear_webhook


def _envolver(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA_ID", "changes": [{"value": value, "field": "messages"}]}],
    }


def _value(messages: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "56911111111", "phone_number_id": "PNID"},
        "contacts": [{"profile": {"name": "Juan Pérez"}, "wa_id": "56912345678"}],
        "messages": messages,
        **extra,
    }


def test_mensaje_de_texto() -> None:
    payload = _envolver(
        _value(
            [
                {
                    "from": "56912345678",
                    "id": "wamid.AAA",
                    "timestamp": "1755259200",
                    "type": "text",
                    "text": {"body": "necesito 200 ampolletas LED"},
                }
            ]
        )
    )

    lote = parsear_webhook(payload)

    assert len(lote.mensajes) == 1
    msg = lote.mensajes[0]
    assert msg.tipo is TipoMensaje.TEXTO
    assert msg.texto == "necesito 200 ampolletas LED"
    assert msg.wa_message_id == "wamid.AAA"
    assert msg.wa_id == "56912345678"
    assert msg.phone_number_id == "PNID"
    assert msg.nombre_perfil == "Juan Pérez"
    assert msg.enviado_at is not None


def test_respuesta_de_boton_conserva_el_id_estructurado() -> None:
    """La confirmación de una propuesta viaja en el id, nunca en el título.

    El título es texto que el usuario ve; el id es lo que correlaciona con la
    propuesta. Confiar en el título rompería con solo cambiar la copy.
    """
    payload = _envolver(
        _value(
            [
                {
                    "from": "56912345678",
                    "id": "wamid.BBB",
                    "timestamp": "1755259300",
                    "type": "interactive",
                    "interactive": {
                        "type": "button_reply",
                        "button_reply": {"id": "confirmar:prop_01H9", "title": "Confirmar"},
                    },
                }
            ]
        )
    )

    msg = parsear_webhook(payload).mensajes[0]

    assert msg.tipo is TipoMensaje.INTERACTIVO
    assert msg.payload_id == "confirmar:prop_01H9"
    assert msg.texto == "Confirmar"


def test_respuesta_de_lista() -> None:
    payload = _envolver(
        _value(
            [
                {
                    "from": "56912345678",
                    "id": "wamid.CCC",
                    "timestamp": "1755259400",
                    "type": "interactive",
                    "interactive": {
                        "type": "list_reply",
                        "list_reply": {
                            "id": "sku:LED-9W-CAL",
                            "title": "Ampolleta LED 9W cálida",
                            "description": "$1.290 c/u",
                        },
                    },
                }
            ]
        )
    )

    msg = parsear_webhook(payload).mensajes[0]
    assert msg.payload_id == "sku:LED-9W-CAL"


def test_boton_de_plantilla() -> None:
    payload = _envolver(
        _value(
            [
                {
                    "from": "56912345678",
                    "id": "wamid.DDD",
                    "timestamp": "1755259500",
                    "type": "button",
                    "button": {"payload": "aprobar:ord_77", "text": "Aprobar"},
                }
            ]
        )
    )

    msg = parsear_webhook(payload).mensajes[0]
    assert msg.tipo is TipoMensaje.BOTON_PLANTILLA
    assert msg.payload_id == "aprobar:ord_77"


def test_nota_de_voz_es_no_soportada_pero_no_revienta() -> None:
    """Las notas de voz son el canal por defecto de mucha gente en venta B2B.

    v1 las rechaza, pero con un tipo explícito para poder responder algo útil.
    """
    payload = _envolver(
        _value(
            [
                {
                    "from": "56912345678",
                    "id": "wamid.EEE",
                    "timestamp": "1755259600",
                    "type": "audio",
                    "audio": {"id": "MEDIA_ID", "mime_type": "audio/ogg; codecs=opus"},
                }
            ]
        )
    )

    msg = parsear_webhook(payload).mensajes[0]
    assert msg.tipo is TipoMensaje.NO_SOPORTADO
    assert msg.subtipo_original == "audio"


def test_imagen_conserva_el_caption() -> None:
    """El caption suele traer la intención real: la foto sola no dice nada."""
    payload = _envolver(
        _value(
            [
                {
                    "from": "56912345678",
                    "id": "wamid.FFF",
                    "timestamp": "1755259700",
                    "type": "image",
                    "image": {"id": "MEDIA_ID", "caption": "cotiza esto x100"},
                }
            ]
        )
    )

    msg = parsear_webhook(payload).mensajes[0]
    assert msg.tipo is TipoMensaje.NO_SOPORTADO
    assert msg.texto == "cotiza esto x100"


def test_tipo_futuro_desconocido_no_rompe_el_parser() -> None:
    """Meta añade tipos sin avisar. Un 500 aquí hace que reintente el webhook."""
    payload = _envolver(
        _value(
            [
                {
                    "from": "56912345678",
                    "id": "wamid.GGG",
                    "timestamp": "1755259800",
                    "type": "holograma_3d",
                    "holograma_3d": {"id": "X"},
                }
            ]
        )
    )

    msg = parsear_webhook(payload).mensajes[0]
    assert msg.tipo is TipoMensaje.NO_SOPORTADO
    assert msg.subtipo_original == "holograma_3d"


def test_mensaje_citado_conserva_el_contexto() -> None:
    payload = _envolver(
        _value(
            [
                {
                    "from": "56912345678",
                    "id": "wamid.HHH",
                    "timestamp": "1755259900",
                    "type": "text",
                    "text": {"body": "de este mejor 300"},
                    "context": {"from": "56911111111", "id": "wamid.ANTERIOR"},
                }
            ]
        )
    )

    msg = parsear_webhook(payload).mensajes[0]
    assert msg.contexto_id == "wamid.ANTERIOR"


def test_varios_mensajes_en_un_solo_post() -> None:
    """Meta agrupa. Dos mensajes rápidos pueden llegar en el mismo POST."""
    payload = _envolver(
        _value(
            [
                {
                    "from": "56912345678",
                    "id": "wamid.1",
                    "timestamp": "1755260000",
                    "type": "text",
                    "text": {"body": "quiero 200 unidades"},
                },
                {
                    "from": "56912345678",
                    "id": "wamid.2",
                    "timestamp": "1755260001",
                    "type": "text",
                    "text": {"body": "del modelo cálido"},
                },
            ]
        )
    )

    lote = parsear_webhook(payload)
    assert [m.wa_message_id for m in lote.mensajes] == ["wamid.1", "wamid.2"]


def test_acuses_de_entrega_no_se_confunden_con_mensajes() -> None:
    payload = _envolver(
        {
            "messaging_product": "whatsapp",
            "metadata": {"phone_number_id": "PNID"},
            "statuses": [
                {
                    "id": "wamid.SALIENTE",
                    "status": "delivered",
                    "timestamp": "1755260100",
                    "recipient_id": "56912345678",
                }
            ],
        }
    )

    lote = parsear_webhook(payload)
    assert lote.mensajes == []
    assert len(lote.estados) == 1
    assert lote.estados[0].estado == "delivered"


def test_status_fallido_conserva_el_error() -> None:
    payload = _envolver(
        {
            "metadata": {"phone_number_id": "PNID"},
            "statuses": [
                {
                    "id": "wamid.X",
                    "status": "failed",
                    "recipient_id": "56912345678",
                    "errors": [{"code": 131047, "title": "Re-engagement message"}],
                }
            ],
        }
    )

    estado = parsear_webhook(payload).estados[0]
    assert estado.estado == "failed"
    assert estado.error is not None
    assert estado.error["code"] == 131047


def test_mensaje_sin_id_se_descarta() -> None:
    """Sin id no hay dedupe posible: mejor descartarlo que procesarlo dos veces."""
    payload = _envolver(
        _value([{"from": "56912345678", "type": "text", "text": {"body": "hola"}}])
    )
    assert parsear_webhook(payload).mensajes == []


def test_payloads_basura_no_lanzan() -> None:
    for basura in (
        {},
        {"entry": None},
        {"entry": [None]},
        {"entry": [{"changes": None}]},
        {"entry": [{"changes": [{"value": "no-es-dict"}]}]},
        {"entry": [{"changes": [{"value": {"messages": ["no-es-dict"]}}]}]},
    ):
        lote = parsear_webhook(basura)
        assert lote.mensajes == []
        assert lote.estados == []
