# VisionServer

Servidor de visión artificial con interfaz web (estilo atvise): corre como servicio en una PC
y se abre desde cualquier computadora de la misma red con el navegador. Publica los resultados
de inspección a SCADA/PLC por **OPC UA** y por **TCP/IP**.

```
 Cámara (USB / IP-RTSP / carpeta / simulada)
        │
        ▼
 ┌─────────────────── VisionServer ───────────────────┐
 │  Captura ─► Motor de inspección ─► Resultado        │
 │                 (herramientas)        │             │
 │        ┌──────────────┬───────────────┼──────────┐  │
 │        ▼              ▼               ▼          │  │
 │   Web :8090      OPC UA :4841     TCP :5000      │  │
 └────────┼──────────────┼───────────────┼──────────┘  │
          ▼              ▼               ▼
   Navegadores       SCADA / PLC      PLC (TSEND/TRCV,
   en la red         (cliente UA)     socket ASCII)
```

## Arranque rápido

1. Doble clic en `iniciar.bat` (la primera vez crea el entorno e instala dependencias).
2. Abre `http://localhost:8090` — o desde otra PC: `http://<IP-de-esta-PC>:8090`
   (la pestaña **Comunicación** muestra las direcciones exactas).
3. Cámara actual: TP-Link Tapo en `rtsp://192.168.1.37:554/stream1`. El usuario y la contraseña
   son la **"Cuenta de cámara"** creada en la app Tapo (Configuración avanzada), no la cuenta TP-Link.
   Para probar sin cámara, elige *Patrón de prueba* en la pestaña *Cámara y disparo*.

Para que arranque solo al iniciar sesión y abrir el firewall (PowerShell como administrador):

```powershell
powershell -ExecutionPolicy Bypass -File .\instalar_servicio.ps1
```

> Para que otras PCs se conecten, la red Wi-Fi debe estar como **Privada** en Windows
> (Configuración → Red e Internet → Wi-Fi → propiedades de la red).

## Usuarios

La interfaz web pide usuario y contraseña. Roles: `admin` (puede cambiar configuración)
y `operador` (monitorea, dispara y cambia de receta). Usuarios actuales: `ALBA` y `Emmanuel` (admin). Las contraseñas se guardan con hash en `users.json`.

```
python tools/usuarios.py listar
python tools/usuarios.py agregar <usuario> admin|operador   (pide la contraseña)
python tools/usuarios.py eliminar <usuario>
```

OPC UA y TCP no piden usuario (los usa el PLC).

## Módulos: Ejecutar / Detener

La interfaz está organizada por módulos; cada uno se ve solo en su pestaña y tiene un botón
**▶ Ejecutar / ■ Detener**. Un módulo detenido no procesa imágenes.

| Módulo | Pestaña | Detenido |
|---|---|---|
| Inspección | *Inspección* | No inspecciona; los disparos del PLC se rechazan (`ERR;INSPECTION_STOPPED`, `Status.Error`) |
| Zonas de seguridad | *Zonas de seguridad* | No ejecuta YOLO; `Safety.Enabled = FALSE` |

El estado se guarda en `config.json`: tras reiniciar el equipo, cada módulo vuelve como estaba.
Solo se cambia con su botón (guardar la configuración no lo modifica). El video solo se transmite
desde la pestaña visible. La única alerta que aparece en todas las pestañas es **persona en zona de peligro**.

Para agregar un módulo nuevo: estado en `config.py`, endpoint en `/api/modules/{nombre}` (`main.py`),
y en la web una `<section>` con `.module-bar` y un botón `.run-btn data-module="nombre"`.

## Herramientas de inspección

| Tipo | Valor que entrega |
|---|---|
| `blob_count` | Número de objetos (umbral fijo u Otsu, filtro de área) |
| `color_area` | % de la ROI dentro de un rango HSV |
| `brightness` | Brillo promedio 0–255 (presencia/ausencia) |
| `person_detect` | Número de personas en la ROI (YOLO) |
| `yolo_detect` | Número de detecciones YOLO de las clases indicadas (`car, bottle, ...`; vacío = todas) |
| `code_reader` | Cantidad de códigos leídos: QR, DataMatrix o barras (EAN, UPC, Code128, Code39, ITF…). Publica el **texto** leído. Opcional: texto esperado (expresión regular) |

Modelos YOLO: la carpeta `models/`. Por defecto `yolo11n.pt` (se descarga solo la primera vez).
Para un modelo entrenado con tus piezas, copia el `.pt` ahí y pon su nombre en la herramienta.

Cada herramienta tiene una ROI (se dibuja con el mouse) y un rango `[Mín, Máx]`: si el valor
cae dentro es OK. El resultado general es OK si todas las herramientas habilitadas son OK.

Para agregar una herramienta nueva: función en `app/vision/tools.py` + registrarla en `TOOLS`.

## Recetas

Cada receta es un programa de inspección (su propio conjunto de herramientas) con un número.
Se cambia desde el selector de la barra superior, desde OPC UA (`Control.RecipeNumber`) o
por TCP (`RECIPE <n>`). En la pestaña *Recetas y herramientas* se crean, duplican y eliminan.

## Zonas de seguridad

Detecta personas con YOLO de forma continua (independiente del disparo) y avisa cuando entran
en zonas poligonales dibujadas sobre la imagen. Cada zona es de **peligro** o **advertencia**.
Modo *pies* (la persona está parada dentro de la zona) o *caja* (cualquier parte del cuerpo).
El retardo al liberar evita parpadeos. Si el monitor falla, reporta `Danger = TRUE`.

> ⚠ Es una función de asistencia. **No** es un sistema de seguridad certificado
> (ISO 13849 / IEC 62061) y no sustituye cortinas de luz, escáneres láser de seguridad,
> relevadores de seguridad ni paros de emergencia.

## OPC UA

Endpoint: `opc.tcp://<IP>:4841/visionserver/` — sin seguridad, acceso anónimo.
Namespace URI: `urn:visionserver`. NodeIds tipo string:

| Nodo | Tipo | Uso |
|---|---|---|
| `VisionServer.Control.Trigger` | Bool RW | El PLC escribe `True` → se inspecciona → el servidor lo regresa a `False` (acuse) |
| `VisionServer.Control.ResetCounter` | Bool RW | Reinicia el contador |
| `VisionServer.Control.RecipeNumber` | Int32 RW | El PLC escribe el número de receta a cargar |
| `VisionServer.Status.ActiveRecipeNumber` / `ActiveRecipeName` | Int32 / String | Receta activa |
| `VisionServer.Status.Heartbeat` | UInt32 | +1 cada segundo (vigilancia de comunicación) |
| `VisionServer.Status.InspectionRunning` | Bool | Módulo de inspección en ejecución |
| `VisionServer.Status.Ready` / `Busy` / `CameraOk` | Bool | Estado |
| `VisionServer.Status.Error` | String | Último error |
| `VisionServer.Result.Counter` | UInt32 | Número de inspección |
| `VisionServer.Result.Pass` | Bool | OK / NOK general |
| `VisionServer.Result.CycleTimeMs` | Double | Tiempo de ciclo |
| `VisionServer.Tools.<Nombre>.Value` / `.Pass` | Double / Bool | Por herramienta |
| `VisionServer.Tools.<Nombre>.Text` | String | Texto leído (lectores de código) |
| `VisionServer.Safety.Enabled` / `Danger` / `Warning` | Bool | Monitor de zonas en ejecución / alarmas |
| `VisionServer.Safety.PersonCount` | UInt32 | Personas detectadas |
| `VisionServer.Safety.Zones.<Nombre>.Occupied` / `.Count` | Bool / UInt32 | Por zona |

Secuencia típica del PLC: esperar `Ready` (solo es TRUE con la inspección en ejecución) → `Trigger := TRUE` → esperar `Trigger = FALSE`
→ leer `Result.Pass` y valores.

## TCP/IP (ASCII)

Puerto 5000. Comandos terminados en CR o CRLF:

```
TRIGGER (o T) → RES;<contador>;<1=OK|0=NOK>;<ciclo_ms>;<herr1>;<herr2>;...
                (los lectores de código envían el texto leído en lugar del número)
GET           → último resultado
STATUS        → READY|STOPPED;<camaraOk 0/1>;<modo>
                (con STOPPED, TRIGGER responde ERR;INSPECTION_STOPPED)
RECIPE <n>    → RCP;<n>;<nombre>   (o ERR;RECIPE_NOT_FOUND;<n>)
RECIPE?       → receta activa
SAFETY        → SAF;<peligro 0/1>;<advertencia 0/1>;<personas>;<zona1 0/1>;<zona2 0/1>;...
RESET         → OK
PING          → PONG
```

Opción *push*: envía cada resultado, y cada cambio en las zonas de seguridad, a todos los clientes conectados.

## Pruebas

Con el servidor corriendo, `python tools/test_clients.py [IP]` simula un PLC por TCP y OPC UA.

## Estructura

```
app/
  main.py              API web, video MJPEG, WebSocket de resultados
  config.py            config.json (se crea con valores por defecto)
  camera.py            fuentes de imagen + hilo de captura
  vision/engine.py     ciclo de inspección (continuo o por disparo)
  vision/tools.py      herramientas de visión (OpenCV, YOLO, códigos)
  vision/yolo.py       carga compartida de modelos YOLO
  vision/safety.py     monitor de zonas de seguridad
  auth.py              usuarios y sesiones
  comms/opcua_server.py
  comms/tcp_server.py
  web/                 interfaz (HTML/CSS/JS sin dependencias)
run.py                 punto de entrada
```
