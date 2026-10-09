# Despliegue

Objetivo final: **Codex Cloud → GitHub → Docker → Ubuntu VPS**.

Codex Cloud es una herramienta de desarrollo. El contenedor desplegado será
autosuficiente y no dependerá de Codex, ChatGPT ni OpenAI para ejecutar producción.
El runtime de trading permanece **STOPPED**. Fase Datos 2 prepara un colector
público independiente y ejemplos systemd; su instalación y activación requieren
una versión aprobada y comprobaciones en el VPS. Esta entrega no los ejecuta.

## Comprobación actual

En la raíz del checkout, con Docker Engine y Compose disponibles:

```sh
docker compose build
docker compose run --rm engine
docker build --target test -t shib-alt-b:test .
docker run --rm --network none shib-alt-b:test
```

El target predeterminado `runtime` instala el proyecto sin dependencias de
desarrollo; `test` instala herramientas y ejecuta la suite básica. El comando
de producción valida la configuración, emite JSON en STOPPED y termina con éxito.
Compose no reinicia procesos ni abre puertos y bloquea red durante la ejecución.
La construcción sí necesita descargar la imagen Python y paquetes desde PyPI.

Si la red de construcción usa una CA corporativa/proxy, proporciona un bundle
de confianza autorizado mediante el secreto temporal `build_ca` de BuildKit:

```sh
docker build --secret id=build_ca,src=/etc/ssl/certs/ca-certificates.crt -t shib-alt-b:runtime .
docker build --secret id=build_ca,src=/etc/ssl/certs/ca-certificates.crt --target test -t shib-alt-b:test .
docker run --rm --network none shib-alt-b:runtime
docker run --rm --network none shib-alt-b:test
```

La ruta debe apuntar al bundle autorizado del host. El montaje solo existe
durante la instalación; no se copia a las capas ni se desactiva TLS. Sin este
secreto se utiliza la confianza normal de la imagen base. En entornos con HOME
de solo lectura, usa `BUILDX_CONFIG=/tmp/shib-alt-b-buildx` al ejecutar builds.

## Fase Datos 2: aplicar una versión aprobada en el VPS

Procedimiento para el operador por SSH en **el VPS real**, nunca desde Codex.
El usuario informa Ubuntu 24.04.4, Python 3.12, `.venv`, deploy key Git de solo
lectura, conectividad con ambos hosts oficiales y una SQLite de marzo de 2024
validada. Estos datos no equivalen a una verificación realizada desde Codex.
No activar trading, endpoints privados ni credenciales Binance.

### 1. Conservar la base y crear un backup antes de actualizar código

Desde `/home/ubuntu/SHib-alt-b`, leer `AGENTS.md`, comprobar `git status`,
`git rev-parse main`, Python 3.12, UTC/NTP (`timedatectl`) y espacio (`df -h`).
Identificar la **ruta absoluta de la SQLite existente**; no asumir la ruta
predeterminada de las plantillas ni crear una base nueva. Detener cualquier job
de datos anterior antes del backup/migración; no debe haber otro escritor.
Reservar al menos el tamaño de una copia completa además de los 512 MiB de
margen; comprobar también `/tmp` y espacio para ZIP, WAL y journald.

La versión Datos 1 todavía no tiene el comando `backup`. Antes de cambiarla,
usar la API estándar de Python para una copia consistente que incluya WAL.
Sustituir únicamente las dos rutas, elegir un **destino nuevo** y ejecutar:

```sh
cd /home/ubuntu/SHib-alt-b
.venv/bin/python - /ruta/absoluta/sqlite-existente.sqlite3 /home/ubuntu/shib-backups/antes-datos2.sqlite3 <<'PY'
import os
import shutil
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

source, target = (Path(p).resolve() for p in sys.argv[1:])
if not source.is_file() or source == target or target.exists():
    raise SystemExit("Origen existente y destino nuevo distintos requeridos")
target.parent.mkdir(parents=True, exist_ok=True)
descriptor, name = tempfile.mkstemp(dir=target.parent, suffix=".part")
os.close(descriptor)
partial = Path(name)
try:
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
        size = src.execute("PRAGMA page_count").fetchone()[0] * src.execute("PRAGMA page_size").fetchone()[0]
        if shutil.disk_usage(target.parent).free < size + 512 * 1024**2:
            raise SystemExit("Espacio insuficiente para backup y reserva")
        if src.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise SystemExit("Origen corrupto; detener despliegue")
        with closing(sqlite3.connect(partial)) as dst:
            src.backup(dst, pages=256, sleep=0.1)
            if dst.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise SystemExit("Backup no íntegro")
    with partial.open("rb") as copied:
        os.fsync(copied.fileno())
    os.link(partial, target)
    directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    print(target)
finally:
    partial.unlink(missing_ok=True)
PY
```

Guardar también una copia fuera del disco del VPS. No copiar solo el `.sqlite3`
con `cp` mientras exista WAL. Nunca borrar, mover ni reemplazar el origen.

### 2. Obtener solo el código aprobado

Después de aprobación y merge a main, sustituir `SHA_APROBADO` por el SHA
exacto comunicado en esa aprobación:

```sh
git fetch origin
git rev-parse origin/main
git status --short
git switch main
git merge --ff-only origin/main
git rev-parse HEAD
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff format --check .
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy
TRADING_MODE=STOPPED .venv/bin/python -m trading_engine
```

**Antes de cambiar main**, comprobar manualmente que `origin/main` coincide con
`SHA_APROBADO` y que el working tree está limpio. Detenerse si difiere. La deploy
key solo requiere lectura; no hacer push desde el VPS. `.gitignore` excluye
`data/`, `historical_data/`, SQLite/WAL/SHM y locks. No usar `git clean -x`,
scripts de limpieza ni comandos que borren directorios de datos. Evitar guardar
la base en archivos rastreados; verificar con `git ls-files` antes de actualizar.

### 3. Revisar configuración e instalar ejemplos, sin activar timers

Leer [DATA_COLLECTOR.md](DATA_COLLECTOR.md). Seleccionar la ruta ya existente en
`SHIB_DATA_DB` de `deploy/systemd/data.env.example`. Revisar propietario ubuntu,
permisos del directorio (SQLite necesita WAL/SHM/lock) y espacio. Si la base vive
en `/home/ubuntu/SHib-alt-b/data/`, conservarla ahí y adaptar **ReadWritePaths**
de todos los servicios a ese directorio; no moverla a `/var/lib` por conveniencia.

Solo tras aprobación operativa, el operador puede copiar las plantillas revisadas
a `/etc/systemd/system/`, y la configuración sin secretos a
`/etc/shib-alt-b/data.env` (root, permisos 0640). No copiar el archivo `.example`
como unidad. Validar las unidades con `systemd-analyze verify`, ejecutar
`systemctl daemon-reload` y comprobar que los timers siguen deshabilitados.
Los ejemplos no crean ni sustituyen la base: `ExecStartPre` exige archivo no vacío.

Usar los comandos CLI con `--db-path` explícito. `history-run --dry-run` no hace
red ni escribe. `collector-status` lee el estado; en la base Datos 1 indicará
que aún falta el gate de validación. Las tablas operativas se crean al abrir un
comando escritor; la migración no modifica filas financieras existentes.

### 4. Importar y validar el histórico, todavía sin timer

Después de comprobar conectividad HTTPS real a ambos hosts oficiales en el VPS,
el operador puede ejecutar:

```sh
sudo systemctl start --no-block shib-data-history.service
sudo systemctl status shib-data-history.service
sudo journalctl -u shib-data-history.service --since today
```

El servicio continúa aunque se cierre SSH. Reutiliza períodos completos y
reanuda después de fallos, sin inventar velas ni sustituir filas. Un 403/451,
checksum incorrecto, conflicto, corrupción o falta de espacio exige intervención;
no eludir restricciones ni restaurar automáticamente el backup sobre el origen.
Un restart automático solo reintenta fallos transitorios, con límite de arranques.
Tras reboot, volver a iniciar el histórico manualmente si estaba incompleto.

Al finalizar, ejecutar `audit-full` y `collector-status` sobre **la misma base**.
Confirmar UTC, identidad, continuidad desde `2024-03-28T07:00:00Z`, taker-buy
real, integridad, ausencia de conflictos y `progress.ready=true`. Comparar el
prefijo validado con el cutoff elegible actual; si pasó tiempo, completar el
tramo pendiente mediante `history-run` antes de autorizar el timer.

### 5. Activación posterior por el operador

Únicamente tras histórico completo/auditado y aprobación operativa:

```sh
sudo systemctl enable --now shib-data-update.timer shib-data-audit.timer
sudo systemctl list-timers 'shib-data-*'
```

Esta documentación no ejecuta esos comandos. El gate también impide que
`update-once` acepte una base nunca validada. Timers persistent recuperan tras
reinicio; el lock del archivo estable evita escritores simultáneos. No borrar el
lockfile. La actualización limita cada invocación a 1500 minutos por defecto;
una desconexión larga se recupera mediante invocaciones sucesivas.

Consultar `collector-status`, journal y espacio; no considerar un job exitoso
como prueba de ausencia de errores de otros jobs. La auditoría diaria puede
coincidir con update y salir 73: repetir al liberar el lock. Crear backups nuevos
con `backup`, verificar su integridad y mantener retención/copia externa bajo
control del operador. Limitar journald según capacidad del VPS. Los límites de
768 MiB/100% CPU protegen recursos; duración y tamaño del histórico real aún
requieren medición en ese VPS.

Ante rollback, detener timers/jobs y conservar la base y el backup. Cambiar solo
el código aprobado; nunca restaurar automáticamente ni eliminar filas. Runtime
STOPPED permanece independiente del colector: no se inicia trading.

Las herramientas directas de desarrollo y construcción tienen versiones fijadas
en `pyproject.toml`. Las dependencias transitivas y el tag `python:3.12-slim` pueden
actualizarse; no se promete una imagen idéntica byte por byte. Una política de
releases reproducibles con hashes/digests se definirá antes del despliegue operativo.
