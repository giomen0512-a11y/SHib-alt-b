# Coordinación temporal — Fase 2

```text
StrategyEngine
      ↓
StrategyResult
      ↓
StrategyCoordinator
      ├─ señales conservadas para el llamador
      └─ pending OrderIntent
                ↓
        not_before alcanzado
                ↓
         due OrderIntent
```

Una única coordinación compartida por los futuros modos. No existe reloj interno:
el tiempo se inyecta. Replay podrá suministrar tiempo simulado; los runners de
Shadow/Live podrán suministrar tiempo real. El coordinador no conoce el retraso
+2 ni calcula delays: recibe `created_at` y `not_before` ya definidos por el llamador.
No hay sleeps, timers, threads, asyncio ni generación de IDs.

## Estado y resultados

`CoordinatorState.pending_intents` es una tupla inmutable de OrderIntent.
Valida tipos, rechaza IntentId duplicados y normaliza el orden canónico:
`(not_before, intent_id.int)`, comparando el valor numérico del UUID para desempatar.
La inserción no afecta el orden, ni siquiera con tiempos iguales. No se obtiene
ningún orden de iteración de estructuras hash.

`CoordinationResult` contiene únicamente `signals` y `state`.
`DueIntentsResult` contiene únicamente `due_intents` y `state`.
Ambos son frozen y validan sus tipos; las intenciones due también son canónicas
y sin duplicados. Los objetos existentes de dominio permanecen inmutables.
No hay historial de señales, rondas, órdenes, balances ni fills en el estado.

## Operaciones

- `StrategyCoordinator(strategy).on_candle(state, candle, position=None,
  round_state=None)` llama exactamente una vez a `StrategyEngine.on_candle`.
  Conserva las señales en el orden recibido y añade intenciones a pending.
  Rechaza duplicados dentro del resultado o contra el estado existente;
  no deduplica ni oculta errores. Incluso una intención inmediata permanece
  pendiente hasta que el llamador avance el tiempo. No crea rondas automáticamente.
- `advance_time(state, now)` exige datetime UTC timezone-aware y separa
  `not_before <= now` (due) de `not_before > now` (pending). Retorna un nuevo
  estado sin los due. No llama al motor de estrategia ni ejecuta intenciones.

`OrderIntent.created_at` indica creación causal; `not_before` es la primera
elegibilidad y debe ser >= created_at. Igualdad expresa ejecución inmediata;
desigualdad expresa diferimiento genérico. No existe el antiguo campo timestamp.
`Order.created_at` debe ser >= intent.not_before: un adaptador no puede aceptar
una orden prematuramente. Todos los tiempos son explícitos y UTC aware.

## Garantías y límites

Con el mismo estado, resultados de estrategia y tiempos, las salidas y el estado
final son iguales. Cada operación retorna datos nuevos sin mutar el estado previo.
El llamador debe conservar el estado retornado: avanzar otra vez usando ese
estado no vuelve a emitir los due retirados. No hay registro de IDs ya retirados,
watermark temporal, reintentos, persistencia ni políticas de ejecución.

Ser due significa elegibilidad, no aceptación ni fill. RiskManager y
ExecutionAdapter mantienen sus contratos, pero el coordinador no los invoca.
El procesamiento operativo de due por esas capas pertenece a una fase posterior.
No se implementan reglas de entrada, estrategias ni modos operativos.
