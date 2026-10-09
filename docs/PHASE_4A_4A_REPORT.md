# Informe de Fase 4A-4A — margen aislado y riesgo

Base: `f2f61330c2b66276af1235c506358d7b2a12eb2a`.
Rama: `phase-4a-4a-isolated-margin-risk`. Runtime STOPPED.
Entrega exclusivamente offline para auditoría. No merge, despliegue, trading,
conexión Binance, liquidación, Add Margin ni históricos descargados.

## 1. Arquitectura del riesgo

Nuevo componente independiente `replay/isolated_margin_risk`, opt-in del mismo
ReplayRunner. Consume ledger confirmado y contexto financiero/Mark causal después
del tick financiero. No cambia AltBEngine, FinancialLedger, FinancialPerformance
ni SimulatedExecution. Desactivado conserva encoding, checksums y fingerprints.

## 2. Contratos de margen

RiskStamp y cuatro evidencias: IsolatedMarginEvidence, MarginAdjustmentEvidence,
MaintenanceBracketSchedule y RiskValuationEvidence; política MarginAllocationPolicy.
UTC económico/disponibilidad, secuencia, identidad, fuente, dataset, versión,
procedencia y supuestos. OBSERVED rechazado sin autenticidad independiente;
UNKNOWN no autoriza montos. Balances absolutos ligados a FillIds/costos exactos.
Reglas completas en [ISOLATED_MARGIN_RISK_SPEC.md](ISOLATED_MARGIN_RISK_SPEC.md).

## 3. Brackets implementados

Decimal floor/cap/rate/cum/max_leverage opcional; metadata y vigencia por schedule.
Rangos contiguos ordenados sin solapamientos; floor inclusivo/cap exclusivo.
Cobertura limitada no extrapolada; saltos de función pueden hacer no verificable
el solver. No se incluyen brackets históricos oficiales ni MMR reales.

## 4. Margen inicial

Regla explícita MODELED: notional de cada Fill BUY / Decimal4. No toma cantidades
solicitadas, ni multiplica otra vez por leverage o1000. BUY10@100 asigna250 USDC.
No acredita una transferencia real ni solvencia del wallet general.

## 5. Asignación aislada

Modo EVIDENCE_ONLY exige balance del instante exacto ligado a fills/costos,
sin proyectar su vigencia hacia el futuro; modo modelado
acumula principal ejecutado y ajustes explícitos. Liberación sólo por evidencia
SELL_RELEASE o política proporcional al principal/venda real. Capital de cuenta,
presupuesto1200, collateral, reserva y equity permanecen separados. Reservas de
órdenes pendientes y capital libre NOT_AVAILABLE; no hay transferencias implícitas.

## 6. Mantenimiento

Notional actual = cantidad abierta × Mark causal. Maintenance = notional × rate
− cum, únicamente con schedule temporalmente válido, recibido y compatible con
4x/símbolo/USDC. Datos faltantes, cap no cubierto o leverage inválido: NOT_AVAILABLE.

## 7. Buffer

Unrealized = Q×Mark−costo abierto; margen = wallet aislado+unrealized; buffer =
margen−maintenance. Ratio sólo con margen positivo. Condición ABOVE / AT / BELOW /
UNKNOWN, disponibilidad AVAILABLE / MODELED / INCOMPLETE / NOT_AVAILABLE / BLOCKED.
Umbral exacto cero no se interpreta como holgura; cantidad cero NO_OPEN_POSITION.

## 8. Umbral modelado

Solver opt-in por bracket: p=(C−W−cum)/(Q×(1−rate)), con W fijo. Recalcula notional,
verifica pertenencia/fronteras y residuo a precisión50, rechaza raíz no positiva,
fuera de cobertura, múltiple o no verificable. Nombre exclusivo
MODELED_MAINTENANCE_THRESHOLD_PRICE; nunca precio oficial de liquidación.

## 9. Aporte hipotético

Solicitud explícita con política lineal: muestra buffer/balance/umbral después
de un importe, sin modificar estado ni emitir transferencias. Mínimo para objetivo
=max(0,objetivo−buffer). Distingue objetivo alcanzado por el importe supuesto y
reserva mínima requerida. Factibilidad UNKNOWN sin reserva/posibilidad explícitas;
una reserva suficiente no garantiza llegada del capital ni éxito de un aporte
menor al mínimo. No se implementa automatización de4A-4B.

## 10. Base shortfill

Conserva cantidad/costo/principal derivados de fills aun cuando P0/DCA/TP están
bloqueados por la política estratégica existente. Cancelar no borra exposición
ni libera principal. Pruebas Base de volumen0 y1 no fabrican completamiento.

## 11. DCA y TP

Ronda sintética Base+7DCA, DCA/TP parciales, TP viejo con fill previo a confirmación
de cancelación, reemplazos y cierre. Exposición, costo y transiciones de margen
coinciden con FillIds del ledger. Órdenes/fills/estado estratégico iguales a la
ejecución sin riesgo; principal liberado exacto al cierre, collateral adicional
separado del PnL realizado y de una liquidación.

## 12. Mark causal

Exclusivamente MarkEvidence reconocido de MarkPriceSource. Recibido hasta el reloj,
vigencia explícita y continuidad declarada. Sin Close, bid/ask, P0, TP, futuro o
interpolación. El calendario pendiente no es evidencia ya reconocida. Pruebas
sin Mark, obsoleto/discontinuo/UNKNOWN y antes del fill conservan ausencia de datos.

## 13. Disponibilidad y conciliación

Fees/funding por ronda reutilizados desde FinancialPerformance; asignación explícita
a la partición aislada, sin descontarlos dos veces del wallet general. Coberturas
faltantes o fee sin asignación impiden wallet conocido. Conflictos de identidad,
económicos, schedules, liberación doble, hechos tardíos o instancia ya valorada
bloquean preservando prefijo. Conflicto de riesgo no altera ledger/performance.
Conflicto financiero bloquea riesgo dependiente.

## 14. Limitaciones respecto de Binance

Evaluación de mantenimiento modelada, no reproducción certificada del exchange.
Sólo muestras temporales: posible incumplimiento intraminuto UNKNOWN; jamás
supervivencia histórica por ausencia de cruces detectados. No liquidaciones,
fills SELL fabricados, transferencias, retenciones/redondeos oficiales ni reservas.
Referencias conceptuales suministradas se citan en la especificación sin descargar
datos ni consultar Binance; no acreditan brackets históricos1000SHIBUSDC.

## 15. Resultados numéricos

| Fixture | Margen inicial | Mark | Unrealized | Balance margen | Maintenance | Buffer |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| A/B BUY10@100 | 250 | 95 | -50 | 200 | 9.50 | 190.50 |
| C | 250 | 75 | -250 | 0 | 7.50 | -7.50 |
| D +100 hipotético | 250 +100 supuesto | 75 | -250 | 100 | 7.50 | 92.50 |

C: BELOW_MAINTENANCE, cero fills de liquidación. D: objetivo100 no alcanzado por
aporte100; mínimo107.50. Buffer E sin Mark y maintenance F sin brackets indisponibles.
G/H concordancia de ronda completa y ausencia de riesgo de liquidación plano.

## 16–18. Validaciones ejecutadas

| Comando | Resultado |
| --- | --- |
| `.venv/bin/pytest -q tests/unit/replay/test_isolated_margin_risk.py tests/unit/replay/test_margin_risk_replay.py` | 116 passed in 112.62s |
| `.venv/bin/pytest -q` | 2064 passed in 473.52s (0:07:53); exit0 |
| `.venv/bin/ruff format --check .` | 147 files already formatted; exit0 |
| `.venv/bin/ruff check .` | All checks passed; exit0 |
| `.venv/bin/mypy` | Success: no issues found in147 source files; exit0 |
| `git diff --check` | Sin salida; exit0 |
| Docker pytest abajo | 2064 passed in 586.93s (0:09:46); exit0 |

Las 116 pruebas nuevas (1948 anteriores preservadas; total2064) incluyen Decimal50/contexto hostil, brackets/fronteras/
raíces múltiples, A–H, costos/asignación, hipótesis/reserva, idempotencia, conflictos,
shortfill, ronda completa, reanudación con pendientes, checksum recalculado e
historia auto-consistente adulterada, gaps/corrupción, desbordamiento Decimal
auditable, ausencia de red y fingerprints.
Ningún test anterior eliminado, modificado o relajado.

Docker utiliza imagen local `shib-alt-b:test`, sin build/pull/instalación:

```bash
env -u DOCKER_HOST -u DOCKER_CONTEXT -u DOCKER_TLS -u DOCKER_TLS_VERIFY \
    -u DOCKER_CERT_PATH docker --host=unix:///var/run/docker.sock run \
    --rm --pull never --user "$(id -u):$(id -g)" --network none --read-only \
    --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges \
    --mount type=bind,src=/workspace/SHib-alt-b,dst=/app,readonly \
    -e PYTHONPATH=/app/src shib-alt-b:test \
    python -m pytest -q -p no:cacheprovider
```

Imagen local SHA-256:
`654de602957ea710db9f76ee67d6de352d36097534e149235da0feb9eb7feccf`.
Comprobación adicional Docker con las mismas restricciones, agregando
`-e TRADING_MODE=STOPPED` y comando `python -m trading_engine`: exit0, evento `bootstrap_ready`, mode `STOPPED`.
Es comprobación local de configuración, no inicio de servicios.

## 19. Archivos

Nuevos:

- `src/trading_engine/replay/isolated_margin_risk/__init__.py`
- `src/trading_engine/replay/isolated_margin_risk/policy.py`
- `src/trading_engine/replay/isolated_margin_risk/evidence.py`
- `src/trading_engine/replay/isolated_margin_risk/models.py`
- `src/trading_engine/replay/isolated_margin_risk/valuation.py`
- `src/trading_engine/replay/isolated_margin_risk/solver.py`
- `src/trading_engine/replay/isolated_margin_risk/core.py`
- `tests/unit/replay/test_isolated_margin_risk.py`
- `tests/unit/replay/test_margin_risk_replay.py`
- `tests/unit/replay/fixtures/isolated_margin_disabled_baseline.json`
- `docs/ISOLATED_MARGIN_RISK_SPEC.md`
- `docs/PHASE_4A_4A_REPORT.md`

Modificados exclusivamente para integración/documentación:

- `src/trading_engine/replay/models.py`
- `src/trading_engine/replay/runner.py`
- `docs/ARCHITECTURE.md`
- `docs/REPLAY_SPEC.md`

## 20–21. Git y comparación

Entrega en un único commit de implementación en feature; publicación mediante
push normal sólo de esa rama. Sin merge, force push ni cambios a main. SHA publicado se entrega
en el informe final y se obtiene con
`git rev-parse phase-4a-4a-isolated-margin-risk`; no se autorreferencia dentro de
su propio commit. Comparación de la entrega contra main:1 commit de implementación sobre la base
aprobada,0 commits divergentes y0 merge commits. La verificación de SHA remoto,
ahead/behind y working tree después del push se consigna en el informe final;
main local/remoto debe permanecer en el SHA base aprobado.

## 22. Pendientes para 4A-4B

Autenticidad/vigencia de datos reales, brackets específicos históricos de USDC,
Mark y cobertura temporal suficiente, reglas de reservas/retenciones/redondeos,
contabilidad entre wallet/particiones, límites y disponibilidad de reserva,
elegibilidad/latencia/confirmación de transferencias e idempotencia. Se requiere
revisión del contrato de automatización antes de implementarlo. STOPPED; detener
para auditoría después de publicación feature. NO MERGE / DESPLIEGUE / TRADING.
