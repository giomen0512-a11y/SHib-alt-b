# Replay — principios futuros

Pendiente de especificación detallada. No hay replay funcional en Fase 0.

- Causalidad estricta: prohibido leer datos futuros.
- Reloj simulado; las decisiones no dependerán del reloj real.
- Determinismo: misma entrada + misma configuración = mismo resultado.
- Checkpoint y reanudación preservando el estado necesario para ese determinismo.
- El mismo StrategyEngine que Shadow y Live; nunca una estrategia independiente.

No se definen todavía formatos, políticas de ejecución ni resultados financieros.
