# Entrar en prisa.cl

prisa.cl corre **OroCommerce** detrás de un WAF con desafío JavaScript. El login
tiene tres barreras encadenadas y ninguna avisa cuando falla: el sitio devuelve
`200` con una página que no es la que pediste, y el error aparece varias capas
más abajo como «no encuentro el campo». Por eso reproducirlo a mano con `curl`
o `httpx` no sale, y por eso este documento existe.

## Las tres barreras

### 1. Desafío JavaScript del WAF

La primera petición a cualquier URL del sitio no devuelve la página, sino ~750
bytes con un script que descifra tres constantes con [slowAES], guarda el
resultado en la cookie `OCXS` y recarga la misma URL con un parámetro añadido.
Sin JavaScript te quedas ahí para siempre.

No hace falta navegador: `slowAES.decrypt(ct, 2, key, iv)` es **AES-128-CBC**
—el `2` es el modo— sobre un solo bloque de 16 bytes, y el resultado se escribe
en hexadecimal **sin quitar relleno**. Está implementado en
`packages/adapters/prisa/desafio.py`, con un vector real en los tests.

> El error tentador es quitar relleno PKCS#7, que es lo que uno hace al ver
> AES-CBC. Da una cookie de 12 bytes que el WAF rechaza, y el síntoma es un
> bucle de desafíos que nunca converge. Hay un test que se pone rojo si alguien
> lo «arregla».

Las constantes cambian en cada respuesta: hay que resolverlo cada vez que
aparece, no cachear la cookie.

### 2. CSRF de Symfony

Hay que hacer `GET /customer/user/login` para obtener el `_csrf_token` del
formulario y la cookie `https-_csrf` que lo acompaña. El POST va a:

```
POST /customer/user/login-check
```

**Con guion.** El valor por defecto de OroCommerce es `login_check` con guion
bajo, que es el que uno escribe de memoria y el que devuelve un 404 silencioso.

Campos: `_username` (RUT con guion y dígito verificador, o correo),
`_password`, `_csrf_token`, `_target_path`, `_failure_path`.

### 3. reCAPTCHA v2 invisible

El formulario incluye un campo `g-recaptcha-response`. Ese token solo lo genera
JavaScript ejecutándose en un navegador real. **Esta es la barrera que hace
imposible el login con un cliente HTTP puro**, por muy bien que resuelvas las
otras dos.

## Cómo se resuelve aquí

Un **Chromium headless normal pasa las tres barreras sin ayuda**. Comprobado
contra el sitio: el reCAPTCHA invisible no levanta reto y el POST llega al nivel
de aplicación. No hace falta ningún servicio de resolución de captchas.

El navegador es para **entrar**, no para quedarse: mantener un Chromium vivo por
petición no se sostiene en un worker. Una vez dentro, prisa.cl es un sitio
normal —cookie de sesión y HTML— y `ClientePrisa` navega con `httpx`,
resolviendo el desafío del WAF de forma transparente cuando reaparece.

```
iniciar_sesion()  ──▶  Chromium  ──▶  cookies  ──▶  ClientePrisa (httpx)
  una vez              WAF+CSRF        OROSFID       todas las peticiones
                       +reCAPTCHA                    siguientes
```

## Uso

```bash
uv sync --extra prisa
uv run playwright install chromium

export PRISA_USUARIO="12345678-9"     # RUT con guion, o correo
export PRISA_PASSWORD="..."

uv run python -m scripts.prisa_login
```

El comando entra, guarda las cookies en `.prisa-sesion.json` con permisos `0600`
y comprueba que la sesión se puede reutilizar desde `httpx`. Si ya hay una
sesión viva no vuelve a entrar: cada login gasta reputación de IP frente al
reCAPTCHA.

### Sin uv

El resto del proyecto usa uv, pero esto no lo necesita. Con `venv` y `pip`,
desde la raíz del repo:

```bash
python3.12 -m venv .venv            # el proyecto pide Python >= 3.12
. .venv/bin/activate

pip install -e ".[prisa]"
playwright install chromium

export PRISA_USUARIO="12345678-9"
export PRISA_PASSWORD="..."

python -m scripts.prisa_login
```

`python3.12` y no `python3`: si el `python3` del sistema es 3.11 o anterior,
`pip install` falla por `requires-python` con un mensaje que habla del paquete
y no de tu intérprete, y se pierde un rato buscando en el sitio equivocado.

Hay que lanzarlo **desde la raíz del repo**: `scripts/` no se instala como
paquete —solo `packages/`—, así que `python -m scripts.prisa_login` depende de
que el directorio actual esté en `sys.path`.

Opciones útiles:

| | |
|---|---|
| `--ver` | abre el navegador con ventana, para ver qué pasa cuando el formulario cambia |
| `--forzar` | entra aunque la sesión guardada siga siendo válida |
| `--arg=...` | bandera extra para Chromium, repetible |

La contraseña se lee del entorno y nunca de un argumento: `ps` y el historial
del shell son públicos dentro de la máquina.

En código:

```python
from packages.adapters.prisa import ClientePrisa, Credenciales, iniciar_sesion

sesion = await iniciar_sesion(Credenciales(usuario="12345678-9", password="..."))
cliente = ClientePrisa(sesion)
pagina = await cliente.obtener("/customer/order/")
```

`ClientePrisa` no renueva la sesión por su cuenta: quién reintenta, y cada
cuánto, es decisión de quien orquesta. `esta_viva()` pregunta al sitio, que es
la única respuesta fiable —OroCommerce caduca por inactividad y no publica ese
plazo—; `Sesion.caducada()` es solo una heurística barata para evitar la
llamada.

## Qué mirar cuando deje de funcionar

| Síntoma | Causa probable |
|---|---|
| `RuntimeError: siguió desafiando` | el WAF cambió el script; resuélvelo con navegador y revisa `desafio.py` |
| `no supe resolver` | mismo caso, pero la forma del script ya no encaja con los regex |
| «no autenticó» sin mensaje del sitio | el formulario cambió: corre con `--ver` y mira |
| «tu cuenta ha sido deshabilitada» | es del lado de Prisa, no del código |
| aparece un reto visual de reCAPTCHA | reputación de la IP — ver abajo |
| `ERR_CONNECTION_RESET` en Chromium | un proxy que retermina TLS no traga el ClientHello post-cuántico. `--arg=--ssl-version-max=tls1.2` |

## Apify

La idea original era usar el [MCP de Apify] para este login. Conviene tener
claro qué hace y qué no: el MCP expone **buscar y ejecutar Actors** de Apify,
consultar su documentación y leer datasets. No hay ningún Actor que sepa entrar
en prisa.cl, y el MCP no aporta nada a las tres barreras de arriba — las pasa un
Chromium normal, gratis.

Donde Apify **sí** ayuda es en la única barrera que puede volverse dura: el
reCAPTCHA puntúa según la reputación de la IP, y desde un datacenter puede
empezar a levantar retos visuales. La respuesta a eso no es cambiar el código,
es cambiar de IP:

```bash
PRISA_PROXY="http://groups-RESIDENTIAL,country-CL:<APIFY_PROXY_PASSWORD>@proxy.apify.com:8000" \
  uv run python -m scripts.prisa_login
```

Eso es el proxy residencial de Apify, que se factura por GB y no necesita el
MCP para nada. La otra cosa que Apify aporta —alojar el navegador— solo hace
falta si no quieres un Chromium en Railway.

El MCP queda configurado en `.mcp.json` por si se quiere explorar el Store
desde Claude Code. Necesita un token en el entorno:

```bash
export APIFY_TOKEN="apify_api_..."
```

Sin `APIFY_TOKEN` el servidor no arranca y Claude Code lo marca como fallido;
no afecta a nada más.

## Nota

El acceso es con la cuenta de Prisa de la empresa y para consultar lo que esa
cuenta ya ve en el navegador. Antes de automatizar volumen sobre el portal
conviene mirar sus términos de uso y, si va a ser recurrente, hablarlo con
Prisa: un acuerdo de integración es más estable que cualquier scraper.

[slowAES]: https://code.google.com/archive/p/slowaes/
[MCP de Apify]: https://docs.apify.com/integrations/mcp
