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

El formulario carga reCAPTCHA (sitekey `6Lf1MuMrAAAAAM5zsVPQxxIND_ZPpfmtkmHJ9by9`)
y ese token solo lo genera JavaScript ejecutándose en un navegador real. **Esta
es la barrera que hace imposible el login con un cliente HTTP puro**, por muy
bien que resuelvas las otras dos.

Ojo al nombre del campo si alguna vez inspeccionas el POST: Google deja su token
en el `g-recaptcha-response` del widget, pero **Prisa lo copia a un campo propio
llamado `google_rechaptcha`** —con la `h` traspuesta— y es ese el que viaja. Un
POST real lleva ahí unos 2.400 caracteres; buscar el nombre estándar da cero y
hace pensar que el captcha no se está resolviendo cuando sí.

Otra cosa que hace el JS del sitio: `rut-view` **reformatea el RUT mientras se
escribe**. Escribir `11111111-1` envía `11.111.111-1`. No hay que normalizarlo a
mano —el navegador hace lo mismo con una persona delante—, pero explica por qué
lo que se envía no es literalmente lo que se escribió. Un correo viaja tal cual.

## Cómo se resuelve aquí

Un Chromium **con ventana** pasa las tres barreras sin ayuda. No hace falta
ningún servicio de resolución de captchas: el reCAPTCHA invisible no levanta
reto y el POST llega al nivel de aplicación con su token de ~2.400 caracteres.

Tras enviar el formulario **se sondea** hasta que aparezca la sesión, no se
espera un rato fijo. Oro envía el login por AJAX y redirige después con
JavaScript, y las páginas de Prisa pasan de 2 MB: mirar una sola vez a los seis
segundos acierta o falla según lo cargada que esté la red. Cuando falla, el
síntoma es el peor posible —el login entra, el navegador se cierra solo y el
comando informa de un rechazo que nunca ocurrió—, y todo apunta a las
credenciales, que es justo donde no está el problema.

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
```

Las credenciales van en `.env`, en la raíz del repo:

```
PRISA_USUARIO=12345678-9        # RUT con guion, o correo
PRISA_PASSWORD=tu$clave!real
```

Y luego:

```bash
uv run python -m scripts.prisa_login
```

**En `.env` y no con `export`.** No es solo convención: dentro de comillas
dobles el shell expande `$` y `!`, así que `export PRISA_PASSWORD="clave$x!"`
guarda otra cosa, y prisa.cl responde «los datos ingresados son incorrectos» —
un mensaje que apunta al sitio equivocado y cuesta un rato descartar. En el
fichero no hay expansión que valga. Si aun así prefieres exportar, comillas
simples.

Cuando el login se rechaza, el comando imprime **cuántos caracteres tenía la
contraseña que envió**. Si ese número no es el que esperas, el problema está en
cómo llegó la variable, no en la cuenta.

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

python -m scripts.prisa_login       # las credenciales, en .env
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
| `--headless` | sin ventana. Solo con un perfil que ya traiga sesión |
| `--forzar` | entra aunque la sesión guardada siga siendo válida |
| `--arg=...` | bandera extra para Chromium, repetible |
| `--manual` | abre el navegador para que entres tú; ver abajo |
| `--espera N` | segundos que aguanta `--manual` (300 por defecto) |

La contraseña nunca se pasa por argumento: `ps` y el historial del shell son
públicos dentro de la máquina.

### Entrar a mano

```bash
uv run python -m scripts.prisa_login --manual
```

Abre una ventana en la página de login y espera. Entras tú, con el ratón, y en
cuanto detecta la sesión se queda con las cookies y cierra. El resultado es
**idéntico** al del login automático —las mismas cookies para `ClientePrisa`—,
así que nada de aguas abajo cambia.

Sirve cuando el login automático no pasa y no está claro por qué: una
credencial que el proceso recibe mal, una verificación nueva, un cambio en el
formulario. Y es la respuesta correcta si algún día Prisa añade segundo factor:
automatizar un 2FA es pelearse con la medida de seguridad, teclearlo una vez
cada varias horas no.

Lo que no hace es sesión eterna. OroCommerce caduca por inactividad, así que un
worker desatendido no puede depender de esto — para eso hace falta que el login
automático funcione.

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
| «no autenticó» sin mensaje del sitio | mira `prisa-fallo.png`, que el propio comando deja. Si en la foto se ve la sesión iniciada, el sitio tardó más que `espera_login_s` |
| «tu cuenta ha sido deshabilitada» | es del lado de Prisa, no del código |
| «los datos ingresados son incorrectos» | credencial, no código. Mira el recuento de caracteres que imprime el propio comando: si no cuadra, el shell mordió la clave — pásala por `.env` |
| aparece un reto visual de reCAPTCHA | reputación de la IP — ver abajo |
| `ERR_CONNECTION_RESET` en Chromium | un proxy que retermina TLS no traga el ClientHello post-cuántico. `--arg=--ssl-version-max=tls1.2` |

## Ya existe esto en ventu 1.0

**Antes de tocar nada aquí, mira `prisa_b2b/` y `base/enrichment/prisa_client.py`
en el repo `ventuglobal/ventu`.** Resuelven las mismas tres barreras, y además:

- `PrisaClient.verify_b2b()` — canario de precios que distingue una sesión B2B
  real de una anónima. Es mejor señal que buscar el enlace de salir: el WAF
  sirve catálogo anónimo con precios anónimos a una sesión caducada, y eso se
  escribiría como si fuera costo de proveedor.
- `prisa_b2b/auth.py::get_session()` — escalera cookies → login HTTP → navegador.
- `base.cookiejar` — las cookies viven en la BD, no en disco, porque en Railway
  el disco se borra en cada deploy.

Lo de este repo se escribió sin saber que aquello existía. Si esto va a
convivir con ventu 1.0, lo sensato es leer el cookiejar compartido en vez de
mantener dos logins.

## Por qué el login automático se abre con ventana

Es el mismo camino que el bot de boletas del SII en ventu 1.0
(`boleta_bot/auth.py::_pantalla`), y por la misma razón.

El intento de login del SII por HTTP puro está **descartado** allí —
`boleta_bot/login_http.py` se conserva "por lo que enseña, no por lo que hace".
Lo que sí funciona es abrir el navegador **con ventana** sobre una pantalla
virtual **Xvfb** que el propio proceso levanta. Queda automático, sin persona
delante, por el mismo camino que ya se sabe que anda.

Aquí igual: `headless=False` es el valor por defecto de `iniciar_sesion`, y en
un servidor sin monitor se levanta Xvfb solo. Hace falta el paquete en la
imagen:

```json
{ "deploy": { "aptPackages": ["xvfb"] } }
```

`--headless` existe, pero solo sirve **con un perfil que ya traiga sesión**: el
login desde cero no pasa sin ventana.

### Perfil persistente y «Recordarme»

Dos cosas hacen que esto aguante desatendido:

- **`PRISA_PERFIL_PATH`** — perfil de Chromium que guarda cookies e historial
  entre corridas. Es lo que hace que el reCAPTCHA deje de tratar cada login como
  un visitante recién llegado, y por lo que en ventu 1.0 el headless funciona
  *después* de que un login con ventana haya sembrado el perfil.
- **«Recordarme»** —en el sitio, «No cerrar sesión»— se marca siempre. Es lo que
  hace que Symfony emita su token persistente, y ese token permite refrescar la
  sesión sin abrir un navegador. La casilla está oculta tras un `<label>`
  estilizado, así que se fuerza y, si no toma, se marca por JS con su evento
  `change`. Después se **relee** el estado: si no quedó marcada, quien lea el
  log tiene que poder distinguir eso de que Prisa no emita el token, porque solo
  el primer caso se arregla desde aquí.

  Comprobado contra el formulario real: la casilla existe, queda marcada y
  `_remember_me=on` viaja en el POST. Aun así **prisa.cl no devuelve token
  persistente**. Es decisión suya, no un fallo de este lado.

  No es grave: sin token, cuando la sesión caduque hay que volver a entrar con
  navegador — pero eso ya es automático gracias a Xvfb, así que sigue sin hacer
  falta una persona. El token solo lo abarataría.

## Los listados no están en el HTML

Esto cuesta una tarde si se descubre por las malas. `/product/search?search=resma`
devuelve 2,5 MB y **ni una resma**: bolígrafos, cafeteras e insecticidas, la
misma lista salga la búsqueda que salga. Es un carrusel de recomendados. Un
parser sobre esa página estaría leyendo recomendaciones y llamándolo catálogo.

Los resultados los pinta un **datagrid de JavaScript** que pide el contenido
aparte. `--estructura` saca su nombre:

```bash
uv run python -m scripts.prisa_ver '/product/search?search=resma' --estructura
#   rejillas    frontend-product-search-grid
```

Y a esa rejilla se le puede preguntar directamente, sin navegador:

```bash
uv run python -m scripts.prisa_ver \
  '/datagrid/frontend-product-search-grid?gridName=frontend-product-search-grid' --ajax
```

**La cabecera `X-Requested-With` es obligatoria** —es lo que hace `--ajax`—. Sin
ella Oro devuelve la página entera en vez de las filas, y el síntoma, 2 MB de
HTML donde se esperaba JSON, se lee como «este endpoint no sirve» cuando lo
único que faltaba era una cabecera.

Lo que hay dentro, medido contra la cuenta real: **9.888 productos**, 20 por
página, con estas columnas entre otras:

```
name  brand  private_label_sku  prices  minimal_price  has_price
availability  low_inventory  image  product_detail  chilecompraId
```

Es decir, el catálogo del proveedor con los precios de la cuenta, en JSON
limpio. Paginación al estilo Oro:

```
&frontend-product-search-grid[_pager][_per_page]=100
&frontend-product-search-grid[_pager][_page]=2
```

**Pendiente:** filtrar. Pasar el término como `[_filter][all_text][value]`
devolvió los 9.888 igualmente, así que Oro lo ignoró — el nombre del filtro es
otro, o la búsqueda entra por un parámetro suelto. Quien lo retome: compare el
`total` con y sin filtro, que es la forma rápida de saber si se está aplicando.

`--ajax` describe la respuesta sin volcarla —cuántas filas, qué columnas,
cuántos registros— porque esas filas son los precios negociados de la cuenta y
esa salida se acaba pegando en un chat o en un issue.

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
