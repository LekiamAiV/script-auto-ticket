# Popup de llamada xCally -> ticket GLPI

Programa residente en Python: cuando entra una llamada en xCally abre **una sola
ventana** con el teléfono ya rellenado, y al enviarla crea el ticket en GLPI con
título, nombre, sucursal, descripción e imágenes adjuntas.

## Archivos

| Archivo | Para qué sirve |
|---|---|
| `xcally_glpi.py` | El programa |
| `config.json` | Toda la configuración (GLPI, sucursales, detección) |
| `config.json.bak` | Respaldo que deja `--autoconfig` |
| `iniciar.bat` | Arranca residente **sin** consola |
| `iniciar_con_consola.bat` | Arranca mostrando el log en vivo (para probar) |
| `llamadas.log` | Registro de llamadas y errores |
| `pendientes/` | Borradores de tickets que no se pudieron enviar |

## 1. Instalación

```bat
pip install requests pillow
```

`pillow` es opcional: solo se usa para pegar capturas de pantalla con `Ctrl+V`.
Sin él, las imágenes se agregan igual con el botón **Agregar...**.

## 2. Configurar `config.json`

Lo mínimo que hay que llenar:

```json
"glpi": {
  "url": "https://glpi.tuempresa.cl/apirest.php",
  "app_token": "...",
  "user_token": "..."
}
```

Cómo obtener los tokens en esta instalación (GLPI 11):

- **App-Token**: `Configuración > General > Clientes de API` — enlace directo
  `/glpi/front/config.form.php?forcetab=Config$8`. El cliente en uso es
  **"Automatizacion Tickets"** (`/glpi/front/apiclient.form.php?id=3`).
  Debe estar **Activo: Sí** y con el rango IPv4 vacío.
- **User-Token**: `Mis preferencias` → pestaña **API** → *Token de API personal*.

Requiere permisos de super admin para ver esas pantallas. Ojo: en esta
instalación *"Habilitar las credenciales del login"* está en **No**, así que
`glpi.user`/`glpi.password` no sirven — hay que usar `user_token`.

### Rellenar el resto solo

```bat
python xcally_glpi.py --autoconfig
```

Lee GLPI y escribe en `config.json` (con respaldo en `config.json.bak`):

- `ui.categorias` — las 7 categorías ITIL con sus IDs
- `ui.sucursales` y `ui.sucursales_locations` — las 121 ubicaciones de GLPI
- `glpi.users_id_requester` — tu ID de usuario (34)

Cuando la sucursal elegida coincide con una ubicación de GLPI, el ticket se
crea además con su `locations_id`. El desplegable acepta texto libre, así que
puedes recortar `ui.sucursales` a las que realmente uses.

Otros campos útiles:

| Campo | Qué hace |
|---|---|
| `glpi.entities_id` | Entidad donde se crea el ticket (0 = Entidad Raíz, como en tu pantalla) |
| `glpi.users_id_requester` | ID del usuario *solicitante* del ticket |
| `glpi.users_id_assign` | ID del *técnico asignado* automáticamente (deja el caso "En curso (asignada)") |
| `glpi.groups_id_assign` | Grupo asignado automáticamente, si lo usas |
| `glpi.requesttypes_id` | Fuente de la solicitud. **3 = Phone**, lo adecuado para llamadas |
| `glpi.itilcategories_id` | Categoría por defecto. Ojo: el campo es **plural** |
| `glpi.type` | 1 = Incidencia, 2 = Requerimiento |
| `glpi.verify_ssl` | Poner `false` si tu GLPI usa certificado autofirmado |
| `ui.sucursales` | Lista del desplegable de Sucursal |
| `ui.sucursales_locations` | Mapa sucursal → `locations_id` de GLPI |
| `ui.entidades` | Opciones del desplegable Entidad |
| `ui.correos_usuarios` | Generado por `--autoconfig`: correo → usuario de GLPI |
| `ui.dominios_correo` | Dominios del desplegable, ordenados por cantidad de usuarios |
| `ui.dominio_por_entidad` | Dominio que se preselecciona por entidad |
| `ui.por_entidad` | Generado por `--autoconfig`: qué es válido en cada entidad |
| `ui.telefono_sucursal` | Mapa teléfono → sucursal: rellena la sucursal sola |
| `ui.categorias` | `{"Redes": 5, "Impresoras": 7}` con los IDs de tus categorías ITIL |
| `ui.title_template` | Plantilla del título. Por defecto `{sucursal} - {resumen}` |
| `listener.token` | Contraseña opcional para el listener local |

Verificar credenciales:

```bat
python xcally_glpi.py --check-glpi
```

## 3. Probar la ventana

```bat
python xcally_glpi.py --test 912345678
```

### Selector de entidad

El formulario tiene un desplegable **Entidad**. Se define en `config.json`:

```json
"ui": { "entidades": { "Entidad Raíz": 0, "CHILE": 3,
                       "Bruno Fritsch": 1, "Difor": 2 } }
```

`--autoconfig` calcula además qué categorías y ubicaciones son **realmente
válidas** en cada entidad, aplicando la regla de GLPI (un elemento se ve en su
propia entidad, y en las hijas sólo si está marcado como *recursivo*). El
resultado queda en `ui.por_entidad`, y al cambiar de entidad el formulario
reajusta los desplegables para que nunca se envíe un ID inválido.

Estado actual de esta instalación:

| ID | Entidad | Categorías | Ubicaciones |
|---|---|---|---|
| 0 | Entidad Raíz | 0 | 1 |
| 1 | Bruno Fritsch | 5 | **0** |
| 2 | Difor | 5 | **0** |
| 3 | CHILE | 5 | 120 |
| 4 | PERÚ | 2 | 0 |
| 5 | PERÚ > Autoland | 2 | 0 |

**Ojo con las ubicaciones:** las 120 ubicaciones (PRT-, ROD-, LA1-…) pertenecen
a la entidad **CHILE (3)** y están marcadas como **no recursivas**, así que no
se ven desde Bruno Fritsch ni Difor. Con esas dos entidades el ticket se crea
igual y la sucursal queda escrita en la descripción y en el título, pero **sin
`locations_id`**. Las 5 categorías sí funcionan, porque están en CHILE y son
recursivas.

**CHILE ya está en el selector** y es la entidad por defecto, justamente porque
es la única donde funcionan categoría y ubicación a la vez. Bruno Fritsch y
Difor siguen disponibles; en esas dos la sucursal va como texto.

Si además quieres la ubicación asociada al elegir Bruno Fritsch o Difor, hay
que marcar esas 120 ubicaciones como **recursivas** en GLPI
(`Configuración > Menús desplegables > Ubicaciones`). Es un cambio en tus datos,
no lo hace el programa.

Y con `Entidad Raíz (0)` no hay ninguna categoría válida, así que el
desplegable de Categoría aparece deshabilitado.

## 4. Detección de llamadas (automática, sin tocar xCally)

**Este es el método que usa el programa por defecto. No requiere Triggers ni
permisos en el servidor xCally.**

La PhoneBar de xCally (`C:\Program Files (x86)\Xenialab s.r.l\XCALLY\PhoneBar.exe`,
la ventana `TopbarWindow`) escribe con log4net en:

```
%USERPROFILE%\xCALLY\Logs\phonebar.log
```

A nivel DEBUG vuelca el JSON completo de cada llamada en dos momentos:

| Momento | Evento en el log | Qué trae |
|---|---|---|
| `ring` — entra a la cola y suena | `OnQueueCall()` | `calleridnum`, `queue`, `uniqueid` |
| `connect` — el agente contesta | `OnWebBrowserIntegration()` | además `membername`, `type`, `calleridname` |

El programa sigue ese archivo, extrae el número (`calleridnum`) y deduplica por
`uniqueid`, así que **una llamada abre exactamente una ventana**, sin importar
cuántos eventos genere.

### Cuándo se abre la ventana

`phonebar.evento_apertura` en `config.json`:

| Valor | Comportamiento |
|---|---|
| `"connect"` *(por defecto)* | Al contestar. Es 100% seguro que la llamada es tuya. |
| `"ring"` | Al empezar a sonar, ~3 s antes. Puedes ir escribiendo mientras contestas, pero se abrirá también en llamadas que termine atendiendo otro agente. |
| `"ambos"` | Abre al sonar; el evento de contestar no reabre nada (dedup por `uniqueid`). |

Medido sobre tu propio histórico (370 MB de logs, 1442 eventos): **731 llamadas
sonaron y 711 las contestaste tú**. Con `"ring"` se habrían abierto ~20 ventanas
de llamadas que atendió otra persona; con `"connect"`, ninguna de más.
Las 34 llamadas **salientes** se descartan solas por `solo_entrantes`.

### Filtros disponibles

```json
"phonebar": {
  "enabled": true,
  "log_file": "",              // vacío = la ruta por defecto de arriba
  "evento_apertura": "connect",
  "solo_entrantes": true,      // ignora las llamadas salientes
  "agente": "",                // ej "mvalenzuelak" (solo aplica al evento connect)
  "colas": [],                 // ej ["Soporte_TI"] — vacío = todas
  "poll_seconds": 1.0
}
```

### Verificarlo

Sobre el histórico, sin abrir nada — lista todas las llamadas que detecta y si
abrirían ventana:

```bat
python xcally_glpi.py --probar-log
```

En vivo, para probar con una llamada real sin que aparezca la ventana:

```bat
python xcally_glpi.py --espiar-log
```

### Requisito

El nivel de log de la PhoneBar debe seguir en `DEBUG` (es el valor de fábrica,
en `PhoneBar.exe.config`). Si alguien lo sube a `INFO` o `WARN`, el evento
`OnQueueCall` desaparece; `OnWebBrowserIntegration` se registra como `INFO` y
seguiría funcionando en modo `"connect"`.

## 5. Alternativa: avisar por HTTP (Triggers de xCally)

Solo si quieres integrarlo desde el servidor. El programa también escucha en
`http://127.0.0.1:8765`; se puede desactivar con `listener.enabled: false`.
La llamada se avisa con:

```
http://127.0.0.1:8765/incoming?number=<NUMERO>&id=<UNIQUEID>
```

- `number` (obligatorio): el número del que llama.
- `id` (recomendado): el `uniqueid` de la llamada. **Es lo que garantiza que la
  ventana se abra una sola vez** durante toda la llamada.
- Opcionales: `name`, `queue`, y `token` si configuraste `listener.token`.

Se aceptan además los nombres nativos de Asterisk/xCally: `calleridnum`,
`callerid`, `uniqueid`, `linkedid`, `from`, `source`. Funciona por GET y por POST
(form o JSON).

### Opción A — Trigger de xCally

Si algún día tienes permisos. En xCally Motion: **Tools > Triggers > Add**

1. **Event**: `Agent Connect` (o `Queue Callback` / `Voice Call` según tu flujo).
   Usa `Agent Connect` para que la ventana se abra recién cuando el agente
   contesta, no en cada rebote de la cola.
2. **Conditions**: filtra por la cola o el agente si corresponde.
3. **Actions > URL Forward / HTTP**:
   `http://127.0.0.1:8765/incoming?number={{calleridnum}}&id={{uniqueid}}&queue={{queue}}`

Las variables entre `{{ }}` son las que ofrece el selector de variables del
Trigger; si tu versión las nombra distinto, usa las equivalentes — el programa
acepta varios alias.

> El Trigger se ejecuta **desde el servidor xCally**. Por eso `127.0.0.1` solo
> funciona si el Trigger se dispara en el PC del agente (MotionBar). Si se
> dispara en el servidor, cambia `listener.host` a `"0.0.0.0"` y apunta el
> Trigger a la IP del PC del agente, con `listener.token` configurado.

### Opción B — MotionBar / navegador

Si usas la MotionBar o un CTI web, configura la acción "abrir URL en llamada
entrante" apuntando al mismo endpoint. Como el navegador hace la petición desde
el propio PC, `127.0.0.1` funciona sin cambios.

### Opción C — Sondear la API de xCally

Si no puedes tocar los Triggers, activa el sondeo en `config.json`:

```json
"xcally": {
  "enabled": true,
  "base_url": "https://xcally.tuempresa.cl",
  "token": "TU_TOKEN_XCALLY",
  "poll_endpoint": "/api/voice/channels",
  "poll_seconds": 3,
  "agent_filter": "tu.usuario"
}
```

`number_fields` e `id_fields` definen de qué campos del JSON sacar el número y
el id; ajústalos según lo que devuelva tu instalación.

### Opcional: liberar la llamada al colgar

Mientras dure la llamada los avisos repetidos se ignoran, y además durante
`call.dedup_seconds` (5 min por defecto). Si quieres que una **nueva** llamada
del mismo número reabra la ventana antes de ese plazo, avisa el corte:

```
http://127.0.0.1:8765/hangup?id=<UNIQUEID>
```

## 6. Dejarlo siempre activo

Copiar un acceso directo de `iniciar.bat` en:

```
%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup
```

(pegar `shell:startup` en Ejecutar para abrir esa carpeta).

## Uso de la ventana

- **Teléfono** llega relleno desde xCally (editable).
- **Sucursal** se rellena sola si el número está en `ui.telefono_sucursal`.
- **Correo**: se escribe **sólo el usuario** y se elige el dominio en el
  desplegable de al lado (`@brunofritsch.cl`, `@difor.cl`, `@bf.cl`). Al lado
  aparece quién quedará como **solicitante** del ticket.
  - El dominio se preselecciona según la **entidad** elegida
    (`ui.dominio_por_entidad`); si lo cambias a mano, deja de moverse.
  - Si el usuario no existe en ese dominio pero **sí en otro**, lo avisa:
    *"no existe; sí está como bgatica@difor.cl (Benjamin Gatica)"*.
  - Si pegas un correo completo en el campo de usuario, se respeta tal cual y
    el desplegable se ignora.
  - **El solicitante del ticket siempre sale de ese correo** (textbox + dominio):
    si el correo pertenece a un usuario de GLPI, queda ese usuario; si no existe
    como usuario, el propio correo queda como solicitante
    (`_users_id_requester: 0` + `alternative_email`), y GLPI lo muestra en
    *Actores > Solicitante* con icono de sobre. Sólo si el campo va **vacío** se
    usa el solicitante por defecto (`glpi.users_id_requester`).
  - No distingue mayúsculas.
  - Resuelve contra los 32 correos que carga `--autoconfig`, sin red; si el
    correo no está en ese mapa, consulta la API por si es un usuario nuevo.
- **Título** se genera automáticamente como `Sucursal - primeras palabras de la
  descripción`. En cuanto lo editas a mano deja de sobrescribirse; el botón
  **Auto** vuelve a generarlo.
- El ticket queda con **Sucursal, Nombre, Teléfono y Fecha de la llamada**,
  seguidos de la descripción. La cola de xCally y el ID de la llamada **no**
  se escriben en el ticket (el ID se sigue usando internamente para no reabrir
  la ventana durante la misma llamada).
- **Imágenes**: botón **Agregar...**, o `Ctrl+V` para pegar una captura de
  pantalla o archivos copiados del Explorador.
- Si GLPI falla, el ticket se guarda en `pendientes/` y se reintenta con:
  `python xcally_glpi.py --retry-pending`

## Comandos

| Comando | Para qué |
|---|---|
| `python xcally_glpi.py` | Modo residente con log en consola |
| `pythonw xcally_glpi.py` | Residente sin consola |
| `--test 912345678` | Abre la ventana con un número de prueba |
| `--probar-log` | Analiza los logs históricos de la PhoneBar |
| `--espiar-log` | Sigue el log en vivo sin abrir ventanas |
| `--check-glpi` | Valida las credenciales de GLPI |
| `--listar-glpi` | Lista entidades y categorías ITIL con sus IDs |
| `--autoconfig` | Rellena categorías, ubicaciones, correos y solicitante desde GLPI |
| `--ping 912345678` | Simula una llamada contra el listener HTTP |
| `--retry-pending` | Reenvía los tickets que fallaron |

## Detalles de la API de GLPI aprendidos aquí

- El campo de categoría del ticket es **`itilcategories_id`** (en plural).
  Si se envía `itilcategory_id`, GLPI responde 200 y **descarta el valor en
  silencio**: el ticket queda sin categoría. Costó detectarlo porque no da error.
- `_users_id_requester` y `_users_id_assign` son campos especiales del `input`;
  el segundo deja el caso en estado *En curso (asignada)*.
- Para poner un correo como solicitante sin que exista el usuario:
  `_users_id_requester: 0` junto con
  `_users_id_requester_notif: {"use_notification": [1], "alternative_email": ["x@y.cl"]}`.
- Los adjuntos van con `multipart/form-data` a `/Document/`, con el JSON en el
  campo `uploadManifest` e `items_id`/`itemtype` apuntando al ticket.
- La visibilidad de categorías y ubicaciones depende de la entidad y del flag
  *recursivo*; ver la tabla de la sección 3.
- Para resolver un correo a usuario: `GET /UserEmail?searchText[email]=...`
  (no distingue mayúsculas) y luego `GET /User/{id}` para el nombre.

## Solución de problemas

| Síntoma | Causa probable |
|---|---|
| `No se pudo abrir el puerto 8765` | El programa ya está corriendo |
| `initSession 401` | `user_token` incorrecto o API de usuario deshabilitada |
| `initSession 400 ... app token` | El cliente API está inactivo, o el `app_token` no coincide. Revisar `/glpi/front/apiclient.form.php?id=3` |
| El ticket se crea pero sin adjuntos | GLPI limita el tamaño/tipo de archivo; ver el detalle en `llamadas.log` |
| El ticket sale sin categoría | Revisar que la categoría exista en la entidad elegida (ver tabla de la sección 3) |
| La ventana no aparece | Revisar `llamadas.log`: si dice "duplicada" o "ya hay una ventana abierta", es la deduplicación funcionando |
| No detecta ninguna llamada | Correr `--espiar-log` durante una llamada real. Si no imprime nada, revisar que `phonebar.log` se esté actualizando y que el nivel siga en DEBUG |
| Se abre en llamadas de otros agentes | Estás en `evento_apertura: "ring"`. Cambiar a `"connect"` |
