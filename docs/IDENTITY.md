# Identidad determinista — schema v1

Política normativa de Fase 2.2, implementada por funciones puras en
`models/identity.py`, compartida posteriormente por Replay, Shadow, Live y recovery.

## Derivación y encoding

`PROJECT_NAMESPACE: Final[UUID]` es la constante
`22842300-09df-530e-93ea-1e985518f6d1`. `IDENTITY_SCHEMA_VERSION: Final[str] = "1"`.
Se calcula `uuid5(PROJECT_NAMESPACE, material)`: UUID versión 5, con el nombre
codificado en UTF-8 por la biblioteca estándar. Cambiar namespace, esquema o
encoding cambia identidades y requiere una migración explícita.

`material` es exactamente `json.dumps(componentes, ensure_ascii=True,
separators=(",", ":"))`, donde componentes es una lista ordenada de strings:

| Helper | Lista canónica, en este orden |
| --- | --- |
| `make_signal_id` | `["id-v1", "signal", strategy_id, symbol.value, checkpoint_at, kind, revision]` |
| `make_round_id` | `["id-v1", "round", signal_id, revision]` |
| `make_action_id` | `["id-v1", "action", round_id, kind, created_at, not_before, revision]` |
| `make_intent_id` | `["id-v1", "intent", round_id, purpose, event_at, causal_ref, revision]` |

El prefijo es `"id-v" + IDENTITY_SCHEMA_VERSION`. No hay campos opcionales
implícitos: revision por defecto es cero y siempre se incluye como `"0"`.
El orden importa; JSON separa y escapa cada componente, incluso delimitadores,
comillas, saltos de línea y Unicode. `ensure_ascii=True` escapa caracteres no ASCII.

- UUID: forma textual lowercase con guiones mediante `str(uuid)`; se exige UUID real.
  Los NewType distinguen IDs nominalmente en mypy, no mediante wrappers en runtime.
- Tiempo: datetime UTC aware validado con las utilidades existentes; formato
  `YYYY-MM-DDTHH:MM:SS.ffffffZ`, siempre seis dígitos de microsegundos.
  Ejemplos: `2026-10-08T14:00:00.000000Z` y `2026-10-08T14:00:00.123456Z`.
  No se aceptan fechas naive ni offsets no UTC, aunque representen el mismo instante.
- Symbol: se exige el objeto Symbol y se usa su valor normalizado.
- Revision: int no negativo, representación decimal; bool, float y strings rechazados.
- Textos: str no vacío ni whitespace-only; se conservan exactamente, sin trimming,
  cambio de case ni normalización Unicode. No hay conversión implícita de tipos.

`make_action_id` exige también `not_before >= created_at`. Los helpers retornan
SignalId, RoundId, ActionId e IntentId respectivamente; no construyen modelos.
Sus firmas tipadas están en identity.py; todos admiten `revision: int = 0`.

## Semántica e idempotencia

Mismo namespace, esquema y componentes canónicos producen el mismo UUID.
Cambiar un componente significativo cambia el material de identidad. La señal
identifica el checkpoint causal, no sus OHLC: revisar datos del mismo checkpoint
no crea automáticamente otra señal. La ronda deriva de SignalId y revision,
permitiendo reconstruir la misma identidad tras un reinicio.

Revision distingue mutaciones legítimamente diferentes por decisión explícita
del dominio. Reprocesar el mismo hecho, un retry ciego o una repetición tras
reinicio **no incrementan revision**. No se implementan incrementos automáticos.

`causal_ref` distingue el hecho que causó una intención sin acoplar identidad
a clases de estrategia. Es texto estable y ya canónico, elegido por la estrategia;
el helper valida que no esté vacío y no interpreta su contenido. Ejemplos futuros:
Base podría referir al ActionId vencido; TP al FillId causal; DCA a una identidad
causal del evento y nivel. Esas selecciones y reglas financieras no se implementan
aquí. No usar texto aleatorio ni timestamps del reloj de sistema.

AltBEngine selecciona los componentes de señales/rondas/acciones en Fase 3B-2B.
Los modelos siguen exigiendo IDs explícitos y StrategyCoordinator no los genera.
OrderId y FillId quedan fuera: la futura capa de ejecución/adaptador resolverá
su identidad y correlación con el proveedor o el modelo de ejecución Replay.

Reconstruir IDs permite futura reconciliación; no implementa deduplicación,
persistencia, recovery operativo, retries ni garantía de ejecución única.

## Componentes Alt B — Fase 3B-2B

Strategy_id canónico `B_DCA7_4X_TP080_AMPLIADO_VPS`, símbolo configurado,
checkpoint causal T y kind `ALT_B_LONG_ENTRY` identifican la Signal con revision 0.
RoundId deriva de ese SignalId. ActionId usa RoundId, kind `ALT_B_BASE_ENTRY`,
created_at=T y not_before=T+120s, también revision 0. Retry, pausa/reinicio y
duplicados no incrementan revision. No se deriva IntentId en esta fase.

Vector congelado para `1000SHIBUSDC`, T=`2026-01-02T12:00:00.000000Z`:

| Entidad | UUID |
| --- | --- |
| SignalId | `562f6059-cb33-5ccc-b208-68a643d24e07` |
| RoundId | `ebd0da91-6e93-58c4-91ca-805a6568b56e` |
| ActionId | `ec7ccf71-85e9-5cc3-b976-fa85b7dd9648` |

AltBStrategyState conserva el cursor de mercado y la reserva lógica para evitar
reemisiones al consumir el estado retornado. Reconstruir desde un snapshot previo
reproduce los mismos IDs, pero registrar/notificar idempotentemente es trabajo
de una futura capa externa. No hay persistencia ni garantía de una ejecución.

## Vectores congelados

Para strategy_id `strategy.example`, Symbol `TESTUSDC`, checkpoint
`2026-10-08T14:00:00.123456Z`, kind `observation` y revision cero:

| Entidad | Componentes adicionales | UUID esperado |
| --- | --- | --- |
| Signal | Los indicados arriba | `b646573c-50f1-51fa-b826-244806b65abc` |
| Round | SignalId anterior, revision cero | `bfe766e7-2758-53b8-9ea0-31603992707b` |
| Action | RoundId anterior, kind `reevaluate`, created_at igual al checkpoint, not_before `2026-10-08T14:00:37.123456Z`, revision cero | `b76f3ed8-1bf5-51ed-bc0c-5798381a0ded` |
| Intent | RoundId anterior, purpose `proposal`, event_at igual al not_before anterior, causal_ref textual del ActionId anterior, revision cero | `c32f10fd-3bf3-58cb-a770-29440deb8d35` |

Los golden tests contienen expectativas literales; no recalculan el esperado
con el helper probado. Protegen namespace, schema, etiquetas, orden y encoding.
