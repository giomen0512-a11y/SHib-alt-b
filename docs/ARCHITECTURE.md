# Arquitectura mínima — Fase 0

Un único núcleo de decisión, con dependencias externas detrás de adaptadores:

```text
MarketDataProvider
    ↓
StrategyEngine
    ↓
Decision / Intent
    ↓
RiskManager
    ↓
ExecutionAdapter
```

Este flujo es un contrato arquitectónico documental, aún sin implementación.
Los futuros adaptadores serán `ReplayExecutionAdapter`, `ShadowExecutionAdapter`
y `BinanceLiveExecutionAdapter`. No se definen interfaces Python hasta contar con
una especificación aprobada que determine sus datos y operaciones.

Replay ≠ estrategia independiente.
Shadow ≠ estrategia independiente.
Live ≠ estrategia independiente.
Los tres compartirán **el mismo StrategyEngine** y la misma política de Add Margin.

## Responsabilidades y dependencias

- `models`: tipos compartidos sin dependencias de proveedores.
- `config`: configuración inmutable, leída del entorno y validada explícitamente.
- `market_data`: futura provisión de datos; no toma decisiones ni ejecuta órdenes.
- `strategy` y `signals`: futuro núcleo de decisión y sus señales; sin llamadas
  a Binance, Telegram, persistencia o adaptadores específicos de un modo.
- `risk`: futura validación de intenciones antes de ejecutar.
- `execution`: futuros contratos de ejecución. Los adaptadores de modo cumplirán
  esos contratos y no contendrán una estrategia duplicada.
- `replay`, `shadow`, `live`: futura coordinación de cada modo y sus adaptadores.
- `add_margin`: futura política compartida, pendiente de especificación.
- `persistence`: almacenamiento separado de las reglas de decisión.
- `binance`, `telegram`: futuras interfaces externas, sin SDK ni acceso en Fase 0.
- `utils`: herramientas generales sin reglas financieras.

## Invariantes

Python 3.12. Precios, cantidades, dinero, PnL, margen y fees utilizarán `Decimal`;
no `float` en cálculos financieros críticos. `to_decimal` admite texto, enteros
y Decimal finitos; rechaza float/bool y no inventa precisión, redondeo o tamaños
de lote. Las políticas de cálculo se especificarán y probarán en su fase.

Timestamps internos UTC timezone-aware. `require_utc` rechaza fechas naive y
offsets no UTC; la normalización de fuentes externas corresponderá a sus adaptadores.

Configuración separada del código y secretos fuera del repositorio. La carga
rechaza modos no implementados o desconocidos en lugar de aceptar una operativa
incompleta. `OperatingMode` enumera estados futuros sin definir transiciones.
En Fase 0 solo `STOPPED` es válido. No existe un interruptor que habilite Live.

El comando `python -m trading_engine` solo valida configuración y registra un
evento JSON con timestamp UTC, nivel, evento y modo. No registra el entorno
ni secretos. Los errores se propagan; no se sustituyen por resultados ficticios.

Tests localizados, cambios pequeños y dependencias justificadas. Los paquetes
reservados contienen solo `__init__.py`; no hay lógica de trading, DCA, TP,
filtros, vetos, replay funcional, Shadow, Live, Add Margin ni Telegram.
