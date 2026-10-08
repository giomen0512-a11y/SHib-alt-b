# SHib-alt-b

Base de un motor de trading modular y testeable en Python 3.12. **Estado: Fase 3B-2A — ingesta incremental y estado reproducible de Alternativa B**.
Incluye infraestructura, modelos, contratos por eventos y coordinación determinista
de acciones programadas separadas de las intenciones ejecutables.
Incluye una política pura UUIDv5 de identidad de dominio con encoding canónico.
Incluye `AltBConfig` y cálculos puros de pesos, márgenes, nocionales, desviaciones
y precios DCA, promedio de fills y TP teórico; sin un StrategyEngine concreto.
La evaluación pura de entrada agrega minutos UTC a bloques 15m y calcula filtros,
vetos A/B y RV poblacional con taker-buy real. Devuelve `EntryEvaluation`;
datos insuficientes bloquean la entrada y las velas futuras se rechazan.
`ingest_closed_1m` recibe y devuelve estado explícito frozen, conserva hasta 1560
minutos y evalúa solo cierres 15m. Un duplicado idéntico no reevalúa; un gap reinicia
el historial y el warmup. Sin persistencia ni integración con StrategyEngine todavía.
**Live NO está implementado**:
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
- `strategy/alt_b`: configuración canónica y matemáticas Decimal de Alternativa B,
  agregación 15m y evaluación causal de entrada con 1560 minutos de warmup,
  ingesta incremental y estado explícito inmutable, precisión local fija de 50
  dígitos; sin señales, redondeo de exchange ni ejecución.
- `orchestration`: estado inmutable, coordinación por eventos y acciones elegibles por tiempo explícito;
  no ejecuta órdenes ni utiliza reloj interno.
- `signals`: reservado; el modelo genérico Signal se encuentra en `models/signals.py`.
- `replay`, `shadow`, `live`, `add_margin`, `persistence`, `binance`, `telegram`:
  paquetes reservados sin implementación.
- `tests/unit`: pruebas de infraestructura y dominio; `tests/integration`: reservado.
- `docs/`: arquitectura, seguridad y especificaciones futuras, fuente de verdad.

Lee [AGENTS.md](AGENTS.md) antes de editar, [arquitectura](docs/ARCHITECTURE.md)
y [modelos de dominio](docs/DOMAIN_MODEL.md) para conocer los contratos e invariantes.
La [coordinación temporal](docs/ORCHESTRATION.md) explica ScheduledAction, pending/due y el flujo de Quote/Fill.
La [identidad determinista](docs/IDENTITY.md) define namespace, schema v1 y helpers de IDs.
La aplicación de producción no depende de Codex,
ChatGPT ni OpenAI.
