# VisionServer

Servidor de visión artificial con interfaz web (estilo atvise): corre como servicio en una PC
y se abre desde cualquier computadora de la misma red con el navegador. Tiene **4 bloques** de
supervisión que corren en paralelo sobre una o varias cámaras, avisa con alarmas (sonido en el
navegador y correo) y publica todo a SCADA/PLC por **OPC UA** y por **TCP/IP**.

```
 Cámaras (USB / IP-RTSP / carpeta / patrón de prueba)
        │
        ▼
 ┌──────────────────────── VisionServer ────────────────────────┐
 │  Captura ─► Zonas de seguridad │ EPP │ ANDON │ Stock ─► Alarmas │
 │        ┌──────────────┬───────────────┬──────────────┐        │
 │        ▼              ▼               ▼              ▼        │
 │   Web :8090      OPC UA :4841     TCP :5000      Correo SMTP  │
 └────────┼──────────────┼───────────────┼───────────────────────┘
          ▼              ▼               ▼
   Navegadores       SCADA / PLC      PLC (TSEND/TRCV,
   en la red         (cliente UA)     socket ASCII)
```

## Arranque rápido

Requiere Windows y **Python 3.14**.

1. Doble clic en `iniciar.bat` (la primera vez crea el entorno `.venv` e instala dependencias).
2. Crea los usuarios (ver *Usuarios*): sin ellos nadie puede entrar a la interfaz.
3. Abre `http://localhost:8090` — o desde otra PC: `http://<IP-de-esta-PC>:8090`
   (la pestaña **Sistema** muestra las direcciones exactas).
4. En la pestaña **Cámaras** agrega la cámara y marca qué bloques analizan su imagen.
   Cámara actual: TP-Link Tapo en `rtsp://192.168.1.37:554/stream1`. El usuario y la contraseña
   son la **"Cuenta de cámara"** creada en la app Tapo (Configuración avanzada), no la cuenta TP-Link.
   Para probar sin cámara, elige *Patrón de prueba*.

Para que arranque solo al iniciar sesión y abrir el firewall (PowerShell como administrador,
después de haber corrido `iniciar.bat` una vez):

```powershell
powershell -ExecutionPolicy Bypass -File .\instalar_servicio.ps1
```

> Para que otras PCs se conecten, la red Wi-Fi debe estar como **Privada** en Windows
> (Configuración → Red e Internet → Wi-Fi → propiedades de la red).

### Lo que no está en el repositorio

| Archivo | Qué es | Cómo se obtiene |
|---|---|---|
| `config.json` | Cámaras (con contraseña), recetas, zonas, correo | Se crea con valores por defecto al primer arranque; se configura desde la web |
| `users.json` | Usuarios de la interfaz (contraseñas con hash) | `tools/usuarios.py agregar ...` |
| `secret.key` | Clave para firmar las sesiones | Se crea sola |
| `models/` | Modelos de detección | Los de Ultralytics se descargan solos la primera vez; `epp_yolov8n.pt` hay que copiarlo a mano |
| `logs/` | Registro del servidor e historial de alarmas | Se crea solo |

## Usuarios

La interfaz web pide usuario y contraseña. Roles: `admin` (puede cambiar configuración)
y `operador` (monitorea, ejecuta/detiene bloques, cambia de receta y silencia alarmas).

```
.venv\Scripts\python.exe tools\usuarios.py listar
.venv\Scripts\python.exe tools\usuarios.py agregar <usuario> admin|operador   (pide la contraseña, mínimo 6 caracteres)
.venv\Scripts\python.exe tools\usuarios.py eliminar <usuario>
```

OPC UA y TCP no piden usuario (los usa el PLC).

## Los 4 bloques

Cada bloque tiene su pestaña, su botón **▶ Ejecutar / ■ Detener**, sus recetas y su alarma.
Un bloque detenido no procesa imágenes. El estado (en ejecución o no) se guarda en `config.json`:
tras reiniciar el equipo, cada bloque vuelve como estaba.

| Bloque | Detecta | Herramienta (por receta) | Cómo |
|---|---|---|---|
| **Zonas de seguridad** | Personas dentro de zonas de peligro o advertencia | Zona: polígono sobre la imagen | YOLO26 (`yolo26n.pt`). Modo *pies* (parado dentro) o *caja* (cualquier parte del cuerpo) |
| **EPP** | Personas sin casco, guantes o lentes (opcional chaleco, cubrebocas, calzado) | Área: rectángulo con su propio equipo requerido. Sin áreas se evalúa toda la imagen | Personas con `yolo26n.pt` + equipo con `epp_yolov8n.pt`. La falta de EPP debe durar `confirm_ms` |
| **ANDON** | Luces roja, amarilla y verde encendidas en torretas; intermitencia y conteo de activaciones | Torreta: rectángulo | OpenCV (color + brillo en HSV), sin red neuronal |
| **Stock** | Cajas de cartón / plástico en racks y estantes | Posición: rectángulo con mínimo/máximo de cajas y si requiere rack | YOLOE-26 (`yoloe-26s-seg.pt`) de vocabulario abierto: las clases se describen en texto |

Cada bloque calcula un nivel: **ninguna**, **advertencia** o **peligro**. Los retardos (`hold_ms`,
`confirm_ms`, `confirm_s`) evitan parpadeos y falsas alarmas (por ejemplo, una persona que pasa
frente al rack). Si un bloque falla, lo reporta como error.

Stock: las clases de texto se pueden cambiar en la interfaz (en inglés, p. ej. `pallet`,
`plastic tote`). La primera vez que cambian se calculan con el codificador de texto
`models/mobileclip2_b.ts` y quedan guardadas en `models/vocab`.

> ⚠ Las zonas de seguridad y el EPP son funciones de **asistencia**. **No** son un sistema de
> seguridad certificado (ISO 13849 / IEC 62061) y no sustituyen cortinas de luz, escáneres
> láser de seguridad, relevadores de seguridad ni paros de emergencia.

### Recetas

Cada receta es un conjunto de herramientas con un número. Se cambia desde el selector de cada
bloque, desde OPC UA (`<Bloque>.RecipeNumber`) o por TCP (`RECIPE <bloque> <n>`).

### Cámaras

Pestaña **Cámaras**: se pueden tener varias. Tipos: `usb` (índice), `url` (RTSP/HTTP), `folder`
(carpeta de imágenes) y `test` (patrón de prueba). En cada cámara se marcan las funciones
(bloques) que analizan su imagen; cada herramienta pertenece a una cámara.

## Alarmas y correo

Cada cambio de nivel de un bloque se registra como evento en `logs/alarmas.csv` (se abre con
Excel) y los últimos se ven en la interfaz. En el navegador suena la alarma (botón 🔊 / 🔇 por
bloque).

Correo (pestaña **Sistema**): Gmail (requiere verificación en 2 pasos y una *contraseña de
aplicación*), Outlook / Microsoft 365 (requiere *SMTP autenticado* en el buzón) o el SMTP de la
empresa. Cada bloque define destinatarios, nivel mínimo, tiempo mínimo entre correos, si adjunta
la imagen anotada y si avisa cuando la alarma se normaliza.

## OPC UA

Endpoint: `opc.tcp://<IP>:4841/visionserver/` — sin seguridad, acceso anónimo.
Namespace URI: `urn:visionserver`. NodeIds tipo string: `ns=<idx>;s=VisionServer.<ruta>`.
La pestaña de cada bloque muestra sus nodos con los valores actuales.

| Nodo | Tipo | Uso |
|---|---|---|
| `Status.Heartbeat` | UInt32 | +1 cada segundo (vigilancia de comunicación) |
| `Status.CamerasOk` | Bool | Todas las cámaras habilitadas tienen imagen |
| `Cameras.<Nombre>.Ok` / `.Fps` | Bool / Double | Por cámara |

Por bloque, `<B>` = `Safety` | `PPE` | `Andon` | `Stock`:

| Nodo | Tipo | Uso |
|---|---|---|
| `<B>.Enabled` | Bool | Bloque en ejecución |
| `<B>.Alarm` / `<B>.Warning` | Bool | Peligro / advertencia |
| `<B>.Level` | Int32 | 0 normal · 1 advertencia · 2 peligro |
| `<B>.AlarmText` / `<B>.Error` | String | Texto de la alarma / último error |
| `<B>.RecipeNumber` | Int32 RW | El PLC escribe el número de receta a cargar |
| `<B>.ActiveRecipeName` | String | Receta activa |
| `<B>.ResetCounters` | Bool RW | `True` → reinicia contadores → vuelve a `False` |

Valores propios de cada bloque y de cada herramienta de la receta activa:

| Bloque | Resumen | Por herramienta |
|---|---|---|
| `Safety` | `Danger`, `PersonCount`, `OccupiedZones` | `Safety.Zones.<Zona>.Occupied` / `.Count` |
| `PPE` | `PersonCount`, `PersonsOk`, `PersonsWithoutPPE`, `NoHelmet`, `NoGloves`, `NoGoggles`, `NoVest`, `NoMask`, `NoShoes`, `ViolationEvents` | `PPE.Areas.<Área>.Ok` / `.Persons` / `.PersonsWithoutPPE` |
| `Andon` | `RedOn`, `YellowOn`, `GreenOn`, `LitCount` | `Andon.Towers.<Torreta>.Red` / `.Yellow` / `.Green` / `.Blinking` / `.LitCount` / `.State` / `.RedCount` / `.YellowCount` / `.GreenCount` |
| `Stock` | `BoxCount`, `RackCount`, `PositionsOk`, `PositionsEmpty`, `PositionsLow`, `PositionsNoRack` | `Stock.Positions.<Posición>.Boxes` / `.RackPresent` / `.Ok` / `.State` |

## TCP/IP (ASCII)

Puerto 5000. Comandos terminados en CR, LF o CRLF; respuestas terminan en CRLF. Separador
configurable (`;`). `<bloque>` = `SAFETY` (o `ZONAS`) | `EPP` | `ANDON` | `STOCK`.

```
SAFETY               → SAF;<peligro 0/1>;<advertencia 0/1>;<personas>;<zona1 0/1>;<zona2 0/1>;...
EPP                  → EPP;<alarma 0/1>;<personas>;<sin_epp>;<sin_casco>;<sin_guantes>;<sin_lentes>;<area1 ok 0/1>;...
ANDON                → AND;<encendidas>;<rojas>;<amarillas>;<verdes>;<torreta1>;<torreta2>;...
                       cada torreta: letras de las luces encendidas R=rojo A=amarillo V=verde, '-' = apagada
STOCK                → STK;<alarma 0/1>;<cajas>;<racks>;<pos1 cajas:estado>;...
                       estado: OK | BAJO | VACIO | EXCESO | SINRACK
(bloque detenido     → ERR;STOPPED;<prefijo>)
STATUS               → STA;<zonas 0/1>;<epp 0/1>;<andon 0/1>;<stock 0/1>;<cámaras con imagen>/<cámaras>
RECIPE <bloque> <n>  → RCP;<prefijo>;<n>;<nombre>   (o ERR;RECIPE_NOT_FOUND;<n>)
RECIPE <bloque>      → receta activa
RESET <bloque>       → reinicia contadores → OK
PING                 → PONG
```

Formato JSON (opcional): el estado completo del bloque en una línea.
Opción *push*: cada cambio en la respuesta de un bloque se envía a todos los clientes conectados.

## Pruebas

Con el servidor corriendo, `.venv\Scripts\python.exe tools\test_clients.py [IP]` simula un PLC
por TCP y OPC UA.

## Estructura

```
app/
  main.py              API web, video MJPEG, WebSocket de estados y eventos
  config.py            config.json (se crea con valores por defecto; migra la versión anterior)
  hub.py               une cámaras, bloques y alarmas
  camera.py            fuentes de imagen + hilo de captura por cámara
  alarms.py            historial de eventos (logs/alarmas.csv) y envío de correos
  notify.py            correo SMTP (Gmail, Outlook, servidor propio)
  auth.py              usuarios y sesiones
  vision/module.py     base común de los bloques (hilo, recetas, nivel de alarma)
  vision/safety.py     zonas de seguridad
  vision/ppe.py        equipo de protección personal
  vision/andon.py      torretas ANDON
  vision/stock.py      cajas en racks
  vision/yolo.py       carga compartida de modelos YOLO / YOLOE y caché por cuadro
  comms/opcua_server.py
  comms/tcp_server.py
  web/                 interfaz (HTML/CSS/JS sin dependencias)
tools/
  usuarios.py          administrar usuarios
  test_clients.py      simulador de PLC (TCP + OPC UA)
run.py                 punto de entrada
iniciar.bat            crea el entorno y arranca
instalar_servicio.ps1  arranque automático + firewall
```

Para agregar un bloque nuevo: subclase de `vision/module.py` (con `process()`, `draw()`, nodos
OPC UA y campos TCP), su entrada en `MODULE_NAMES` y `DEFAULT_CONFIG` (`config.py`) y en
`Hub` y `ALIASES` (`hub.py`), su comando en `TCP_COMMANDS` (`main.py`) y su pestaña en `web/index.html`.
