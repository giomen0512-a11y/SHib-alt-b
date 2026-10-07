# SHib-alt-b

Base de un motor de trading modular y testeable en Python 3.12. **Estado: Fase 1**.
Incluye infraestructura, modelos de dominio y contratos mínimos. **Live NO está implementado**:
el único modo admitido es `STOPPED`. No conecta con Binance ni requiere secretos.

## Requisitos e instalación

Python 3.12 con `venv` y `pip`. Docker con Compose es opcional para uso local.
Desde la raíz del repositorio:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m trading_engine
```

El último comando valida configuración, emite un evento JSON `bootstrap_ready`
en `STOPPED` y termina. No inicia un motor operativo.
La configuración se lee del entorno del proceso; `.env.example` es una plantilla
sin secretos. Python no carga `.env` automáticamente. No se requieren variables
para la comprobación inicial; cualquier `TRADING_MODE` distinto de `STOPPED` falla.

## Validación

```sh
.venv/bin/python -m ruff format --check .
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy
.venv/bin/python -m pytest -q
```

Para aplicar formato: `.venv/bin/python -m ruff format .`.

## Docker

```sh
docker compose build
docker compose run --rm engine
docker build --target test -t shib-alt-b:test .
docker run --rm --network none shib-alt-b:test
```

La imagen predeterminada instala únicamente producción; el target `test` añade
pytest, mypy y Ruff. Ambos usan Python 3.12 y un usuario sin privilegios.
Compose ejecuta la comprobación de configuración sin red y termina; no hay servicios
adicionales. Consulta [despliegue](docs/DEPLOYMENT.md).

## Estructura

- `src/trading_engine/config`, `models`, `utils`: configuración, modelos, estados y utilidades.
- `market_data`, `strategy`, `risk`, `execution`: Protocols mínimos del motor.
- `signals`: reservado; el modelo genérico Signal se encuentra en `models/signals.py`.
- `replay`, `shadow`, `live`, `add_margin`, `persistence`, `binance`, `telegram`:
  paquetes reservados sin implementación.
- `tests/unit`: pruebas de infraestructura y dominio; `tests/integration`: reservado.
- `docs/`: arquitectura, seguridad y especificaciones futuras, fuente de verdad.

Lee [AGENTS.md](AGENTS.md) antes de editar, [arquitectura](docs/ARCHITECTURE.md)
y [modelos de dominio](docs/DOMAIN_MODEL.md) para conocer los contratos e invariantes.
La aplicación de producción no depende de Codex,
ChatGPT ni OpenAI.
