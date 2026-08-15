"""Alta declarativa de remitentes autorizados.

`ventupilot.clientes` arranca vacía a propósito (invariante 4): nadie se
auto-registra. Eso deja una pregunta operativa — cómo se da de alta a alguien
— que hasta ahora no tenía respuesta, porque la Postgres de ventu-prod solo es
alcanzable desde dentro de la red privada de Railway y no hay shell donde
correr un `INSERT`.

La respuesta es esta: una variable de entorno declarativa que el worker aplica
al arrancar. Declarativa y no imperativa porque así el estado deseado está a la
vista en el panel de Railway, y no escondido en un comando que alguien corrió
una vez y nadie recuerda.

Formato de `CLIENTES_AUTORIZADOS`:

    +56 9 6626 6451:consultar,cotizar; 56911112222:consultar

Entradas separadas por `;`, y en cada una `numero:permisos`. El número se
normaliza (se le quitan `+`, espacios y guiones) porque Meta manda el `wa_id`
sin nada de eso, y un `+` de más produce un hash distinto y un "no autorizado"
inexplicable. Los permisos son opcionales; sin ellos se concede `consultar`.

Lo que esto **no** hace: revocar. Quitar a alguien de la variable no lo
desactiva, porque un despliegue con la variable mal copiada no debe cortarle el
acceso a nadie en silencio. Revocar es explícito, con `ClientesRepo.desactivar`.
"""

from __future__ import annotations

import logging

from packages.adapters.clientes import ClientesRepo
from packages.adapters.whatsapp.identity import enmascarar, hash_wa_id
from packages.domain.identidad import Permiso

log = logging.getLogger("altas")

_PERMISOS_POR_DEFECTO = frozenset({Permiso.CONSULTAR})


def normalizar_numero(valor: str) -> str:
    """Deja el número como lo manda Meta: solo dígitos.

    `+56 9 6626-6451` → `56966266451`. Sin esto, el hash no coincidiría con el
    del `wa_id` entrante y el remitente quedaría fuera sin motivo aparente.
    """
    return "".join(c for c in valor if c.isdigit())


def parsear(declaracion: str) -> list[tuple[str, frozenset[Permiso]]]:
    """Convierte la variable de entorno en pares (numero, permisos).

    Tolerante con el formato —espacios, entradas vacías, permisos en
    mayúsculas— porque esto se edita a mano en un panel web. Lo que no tolera
    es un permiso desconocido: se ignora con un aviso, en vez de concederlo o
    de reventar el arranque del worker por una errata.
    """
    salida: list[tuple[str, frozenset[Permiso]]] = []

    for entrada in declaracion.split(";"):
        entrada = entrada.strip()
        if not entrada:
            continue

        numero_txt, _, permisos_txt = entrada.partition(":")
        numero = normalizar_numero(numero_txt)
        if not numero:
            log.warning("entrada sin número válido en CLIENTES_AUTORIZADOS; se ignora")
            continue

        permisos = set()
        for p in permisos_txt.split(","):
            p = p.strip().lower()
            if not p:
                continue
            try:
                permisos.add(Permiso(p))
            except ValueError:
                log.warning("permiso desconocido %r para %s; se ignora", p, enmascarar(numero))

        salida.append((numero, frozenset(permisos) or _PERMISOS_POR_DEFECTO))

    return salida


async def aplicar(repo: ClientesRepo, declaracion: str, pepper: str) -> int:
    """Da de alta lo declarado. Devuelve cuántos se aplicaron.

    Idempotente: `autorizar` hace UPSERT por `wa_id_hash`, así que arrancar el
    worker cien veces deja el mismo estado.
    """
    entradas = parsear(declaracion)
    for numero, permisos in entradas:
        await repo.autorizar(hash_wa_id(numero, pepper), set(permisos))
        # Número enmascarado: nada de PII en logs (invariante 8).
        log.info(
            "cliente autorizado %s con %s",
            enmascarar(numero),
            ",".join(sorted(p.value for p in permisos)),
        )
    return len(entradas)
