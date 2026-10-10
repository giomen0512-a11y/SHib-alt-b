# Checkpoints de verificación y consultas — Fase 4A-5C

Offline opt-in. Runtime STOPPED. Ninguna autenticación o integración financiera.
Complementa [HISTORICAL_FAST_QUERY_SPEC.md](HISTORICAL_FAST_QUERY_SPEC.md).

## VerificationCheckpoint

Frozen: selection_id; DatasetIds/AuditIds; source_digests; SegmentIds/digests;
verified_ranges; verification_mode/scope; session_identity; root_id; source_state;
original_state; versiones de schema del registro y parser. Expresa qué segmentos
se comprobaron bajo una raíz de sesión derivada de verificación integral inicial.
Los segmentos y sus rangos son verificables, acotados y vinculados al original.
La cache tiene versión propia; no migra ni sobrescribe registry versión1.

No se etiqueta una consulta parcial como FULL_SOURCE_VERIFIED. Durante consultas
rápidas source_state=SEGMENT_VERIFIED y original_state=SOURCE_NOT_REVALIDATED.
La raíz es un ancla de **integridad local relativa a selección congelada**;
no una prueba independiente de que Binance publicó esos datos. Tampoco protege
si un adversario controla tanto proceso como raíces. No se crean firmas simuladas.

## QueryRecoveryCheckpoint

Frozen: VerificationCheckpoint; kind/start/end/cutoff; cursor ordinal de salida;
source_cursors por fuente; current_segment; last_verified_event; digest causal
acumulado; digest de preflight; conflictos reconocidos en preflight; stream_state;
checkpoint_digest. El cursor de salida cuenta items de auditoría, incluidos conflictos.
Cursores de fuente/último evento económico representan el último hecho reconocido:
los conflictos se conservan aparte; no se transforman en hechos económicos.
Cursor/preflight son de consulta, no cursores de Replay ni commits financieros.

El digest causal usa encoded(HistoricalEvent.economic_key()) igual que 4A-5B;
no incluye SHA global, DatasetId o futuros. El preflight sí vincula raw, fuentes,
segmentos y metadatos necesarios para reconstruir exactamente esa consulta.
Un checkpoint emitido durante entrega sigue siendo PROVISIONAL; no acredita
finalización. FAILED no permite emitir checkpoint. COMPLETE exige consumo total
con verificación final exitosa; INTERRUPTED expresa cierre sin certificación final.

checkpoint() sella una nueva versión frozen; el caller conserva las anteriores.
El almacenamiento de checkpoints emitidos se acota por max_queries de la política.
No se inventan cursores futuros ni se autoriza editar objetos sellados.

## Raíces de confianza y adulteración

Un checksum recalculable no basta. En la misma sesión, resume exige coincidencia
exacta con un checkpoint **emitido** cuyo contenido está retenido en memoria.
Una versión alterada, aunque recalcule checksum, no está emitida y se rechaza.
Cambiar selección, original/digest, DatasetId/AuditId, parser/schema, segmentos,
intervalo/cutoff, preflight, digest causal, último evento, fuentes o cursor bloquea.

Tras reiniciar, no existe la memoria de emisión ni se reutilizan sellos del disco
como prueba autosuficiente. El caller debe suministrar trusted_checkpoint_digest
obtenido de una referencia **independientemente protegida** del JSON del checkpoint.
No debe tomarlo del propio archivo no confiable ni recalcularlo para aprobar una
versión manipulada. Si falta esa raíz, INDEPENDENT_CHECKPOINT_ROOT_REQUIRED.
Este contrato no proporciona un almacén autenticado externo; su provisión segura
es responsabilidad explícita del caller. No hay llaves, credenciales o firma Binance.

Además, el proceso nuevo revalida íntegramente todos los originales y registro y
reconstruye el índice. No hereda confianza de sesión anterior. Recalcula preflight,
recorre hasta el cursor y reconstruye digest/último evento/cursores/conflictos. Compara
campos completos con el checkpoint anclado. Sólo puede variar session_identity
cuando hay nueva raíz de proceso; fuentes, parser, rangos y root_id deben coincidir.
Un hash del propio checkpoint no se usa como sustituto de esta reconstrucción.

## Reanudación exacta

resume(cp) conserva el bloqueo económico de read: conflictos aplicables abortan
antes de entrega. resume_groups(cp) permite reanudar la **auditoría** de eventos
más conflictos; no elimina ni resuelve contradicciones. Ambos reconstruyen
preflight/cursor y devuelven únicamente items posteriores al reconocido.
Se conserva orden, EventIds, economía, timestamps, procedencia y digest final.
No hay repetición, omisión o anticipación. Los cursores no se aceptan sólo por estar
dentro del tamaño del archivo: requieren raíz de emisión/externa y reconstrucción.

La recuperación vuelve a verificar los segmentos solicitados (no sólo los items
restantes). En sesión warm ese coste depende de segmentos, no del original entero.
Reabrir proceso vuelve a pagar verificación integral e índices. No es recovery LIVE,
no hay scheduler financiero, recuperación de servicios ni conexión a ReplayRunner.

encoded(cp) produce JSON canónico. checkpoint_from_json aplica tamaño máximo1MiB,
colecciones acotadas, enums, UTC, constructor frozen y cursor int real; rechaza NaN,
keys duplicadas y campos ajenos. **Decodificar no valida la confianza**. No usa pickle,
eval o código remoto. Checkpoints generados, referencias y caches quedan fuera de Git.

## Flujo de auditoría

```python
stream = query.read(kind, start, end, cutoff=cutoff)
first = next(stream)  # Sigue PROVISIONAL.
cp = stream.checkpoint()
stream.close()
restored = query.resume(cp)  # Misma raíz de emisión en memoria.
for candidate in restored:
    review(candidate)
assert restored.state == "COMPLETE"
```

El consumidor conserva también el prefijo reconocido; no debe duplicarlo.
Un checkpoint representa progreso de lectura, no atomicidad económica del consumidor.
Una integración posterior requerirá un contrato separado de commit/rollback de
consumidores, autenticación, correcciones versionadas y evidencia temporal oficial.

## Pruebas y límites

A–P: cambio antes/después de preparación; segmento consultado/no consultado;
registro, índice de selección y sellos adulterados; checkpoints rehasheados;
identidad/selección/parser/schema diferentes; truncación/sustitución/ausencia;
ZIP corrupto; cambio durante stream. Se prueba que un cambio oculto fuera del
segmento puede pasar en modo parcial con SOURCE_NOT_REVALIDATED, mientras STRICT_FULL
lo detecta. No se presenta esa falta de detección global como garantía equivalente.

Fixtures1/7/30 días: continuo y reanudado antes de primer registro, dentro de
segmento, en frontera, antes de finalización y durante groups usado en conciliación.
Comparación de economía, orden, cobertura/conflictos y digest; restart verdadero
en subprocess con raíz independiente y validación integral nueva. Fallo de preparación
no publica cache; errores/cierre eliminan artefactos temporales. Límites de archivos,
ZIP, filas, memoria y hallazgos previos siguen activos; no se relajan tests anteriores.
