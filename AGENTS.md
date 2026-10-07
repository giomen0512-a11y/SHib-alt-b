# Reglas de trabajo

- Lee primero este archivo y solo la documentación pertinente a la tarea. Evita recorrer todo el repositorio sin necesidad.
- Identifica el scope mínimo antes de editar. Haz cambios localizados, sin refactors oportunistas ni cambios innecesarios de interfaces públicas.
- Las especificaciones en `docs/` son la fuente de verdad. Si contradicen el código, detente y señala la contradicción; no inventes comportamiento.
- No anticipes fases futuras, dupliques lógica ni añadas dependencias sin justificación.
- Usa Python 3.12, `Decimal` para valores/cálculos financieros y timestamps UTC timezone-aware.
- No cambies comportamiento financiero sin tests. Nunca borres o relajes tests para hacerlos pasar.
- En cambios pequeños, ejecuta primero los tests del módulo afectado; no replays históricos completos.
- Mantén separados estrategia, datos, riesgo, ejecución, persistencia e interfaces externas. Replay, Shadow y Live compartirán un único StrategyEngine.
- Configuración fuera del código; secretos fuera de Git. No registres secretos ni ocultes errores silenciosamente. Usa logging estructurado.
- Nunca habilites Live por defecto: debe permanecer fail-closed. En Fase 0 solo se admite STOPPED.
- Mantén commits pequeños y descriptivos. Cada entrega debe indicar archivos modificados y tests ejecutados.
- Producción debe funcionar sin Codex, ChatGPT u OpenAI.
