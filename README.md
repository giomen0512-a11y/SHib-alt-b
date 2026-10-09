# SHib-alt-b

Base de un motor de trading modular y testeable en Python 3.12. **Estado: Fase 3B-2B — señales y scheduling Alt B, sin ejecución**.
Incluye infraestructura, modelos, contratos por eventos y coordinación determinista
de acciones programadas separadas de las intenciones ejecutables.
Incluye una política pura UUIDv5 de identidad de dominio con encoding canónico.
Incluye `AltBConfig` y cálculos puros de pesos, márgenes, nocionales, desviaciones
y precios DCA, promedio de fills y TP teórico.
La evaluación pura de entrada agrega minutos UTC a bloques 15m y calcula filtros,
vetos A/B y RV poblacional con taker-buy real. Devuelve `EntryEvaluation`;
datos insuficientes bloquean la entrada y las velas futuras se rechazan.
`ingest_closed_1m` recibe y devuelve estado explícito frozen, conserva hasta 1560
minutos y evalúa solo cierres 15m. Un duplicado idéntico no reevalúa; un gap reinicia
el historial y el warmup. AltBEngine integra esa evaluación con Signal UUIDv5 y
ScheduledAction a +120s, con estado explícito frozen y control fail-closed.
Sin OrderIntent, Quantity ni ejecución; ver [señales Alt B](docs/ALT_B_SIGNALS.md).
No se implementa persistencia del estado estratégico.
La CLI manual de datos permite archivos oficiales mensuales/diarios y REST público
USDⓈ-M 1m, SQLite exacto, sincronización puntual y auditoría/recuperación de gaps.
Consulta [pipeline de datos](docs/DATA_PIPELINE.md); no descarga al iniciar el bot.
La operación autónoma reutiliza ese pipeline con bloqueo, checkpoints, update-once
acotado, auditoría, backup y ejemplos systemd sin activar; ver
[colector](docs/DATA_COLLECTOR.md). No depende de Codex ni de SSH conectado.
**Live NO está implementado**:
el único modo admitido es `STOPPED`. El bot no conecta con Binance ni requiere secretos;
solo los comandos de datos pueden consultar fuentes públicas.

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
- `strategy`, `risk`, `execution`: Protocols mínimos del motor.
- `strategy/alt_b`: configuración canónica y matemáticas Decimal de Alternativa B,
  agregación 15m y evaluación causal de entrada con 1560 minutos de warmup,
  ingesta incremental y estado explícito inmutable, precisión local fija de 50
  dígitos; sin señales, redondeo de exchange ni ejecución.
- `orchestration`: estado inmutable, coordinación por eventos y acciones elegibles por tiempo explícito;
  no ejecuta órdenes ni utiliza reloj interno.
- `signals`: reservado; el modelo genérico Signal se encuentra en `models/signals.py`.
- `market_data`: además del contrato, CLI y coordinación manual del histórico público.
- `binance`: transporte público y normalización Futures; `persistence`: histórico SQLite.
- `replay`: runner causal offline con SQLite read-only, reloj simulado, puertos
  explícitos de Quote/Order/Fill, snapshot integral y ledger de PnL bruto opt-in.
- `shadow`, `live`, `add_margin`, `telegram`: paquetes reservados sin implementación.
- `tests/unit`: pruebas de infraestructura, dominio y datos, incluida integración SQLite de 27h.
- `docs/`: arquitectura, seguridad y especificaciones futuras, fuente de verdad.

Lee [AGENTS.md](AGENTS.md) antes de editar, [arquitectura](docs/ARCHITECTURE.md)
y [modelos de dominio](docs/DOMAIN_MODEL.md) para conocer los contratos e invariantes.
La [coordinación temporal](docs/ORCHESTRATION.md) explica ScheduledAction, pending/due y el flujo de Quote/Fill.
La [identidad determinista](docs/IDENTITY.md) define namespace, schema v1 y helpers de IDs.
La aplicación de producción no depende de Codex,
ChatGPT ni OpenAI.

## Replay causal — Fase 4A-1

Consulta [REPLAY_SPEC.md](docs/REPLAY_SPEC.md) para el uso offline de biblioteca,
la secuencia de eventos, ausencia de Quotes y reanudación dentro del proceso.
El runtime continúa STOPPED. Los resultados financieros no están disponibles.

## Modelo de precios — Fase 4A-2A

[EXECUTION_MODEL_SPEC.md](docs/EXECUTION_MODEL_SPEC.md) describe las Quotes
MODELED_FROM_1M, parámetros obligatorios y escenarios de sensibilidad reproducibles.
No se implementan fills históricos ni PnL; el runtime permanece STOPPED.

## Simulador offline — Fase 4A-2B

[SIMULATED_EXECUTION_SPEC.md](docs/SIMULATED_EXECUTION_SPEC.md) documenta la ejecución
opt-in de OrderIntent/CancelRequest con hechos MODELED_EXECUTION, latencias y
volumen compartido. El mismo AltBEngine gestiona DCA/TP y cierre. Runtime STOPPED;
no hay trading real ni resultados financieros.

## Ledger offline — Fase 4A-3A

[FINANCIAL_LEDGER_SPEC.md](docs/FINANCIAL_LEDGER_SPEC.md) documenta el ledger opt-in
de FillEvidence recibidos: cantidades, costo promedio móvil, PnL bruto por ronda,
conciliación y reanudación. Las huellas anteriores se conservan sin ledger.
Neto, fees/funding, equity/drawdown y liquidación siguen no disponibles.
Runtime STOPPED; sin trading, servicios ni despliegue.

Fase 4A-3B añade contabilidad offline **opt-in** de fees/funding, wallet, Mark,
PnL flotante, equity y drawdown, condicionada a evidencia y cobertura explícitas.
Sin esas fuentes, las métricas dependientes permanecen NOT_AVAILABLE. El ledger
bruto y las huellas anteriores se conservan cuando está desactivada. Fixtures y
escenarios no son resultados históricos certificados de Binance. Véase
[FINANCIAL_PERFORMANCE_SPEC.md](docs/FINANCIAL_PERFORMANCE_SPEC.md).
