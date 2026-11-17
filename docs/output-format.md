# Contrato de salida V2 — breve y accionable

Todos los agentes y skills de la V2 siguen este contrato. Objetivo: **el usuario lee en 30 segundos qué hacer**, y el detalle queda en ficheros para quien lo quiera.

Idioma por defecto: **español**. (Los informes internos de agentes pueden ir en inglés o español; el brief al usuario siempre en español salvo que el usuario escriba en otro idioma.)

---

## 1. Informe de analista (fichero en `data/reports/YYYY-MM-DD-{SYMBOL}/<rol>.md`)

Máximo **~40 líneas** de prosa/tablas + un bloque JSON final. Nada de repetir datos crudos que no cambian la decisión.

```markdown
# <Rol> — <SYMBOL> — <fecha UTC hora>
**Lectura:** <1 frase: qué dice este análisis>
**Sesgo:** ALCISTA | BAJISTA | NEUTRAL  ·  **Confianza:** 1-10

## Datos clave
| Métrica | Valor | Lectura |
|---|---|---|
(5-10 filas máximo, solo lo que mueve la decisión)

## Niveles / setups
(solo si aplica al rol: soportes, resistencias, invalidación, setups con entrada/SL/TP)

## Riesgos y datos faltantes
- (máx. 4 viñetas)

```json
{"role": "technical-analyst", "symbol": "BTC", "bias": "bearish", "confidence": 6,
 "key_levels": {"support": [62000, 57800], "resistance": [67000, 72900]},
 "setups": [{"book": "futures", "side": "long", "entry": [63000, 64500], "sl": 61600, "tp": [67500, 71200]}],
 "signals": {"regime": "strong_downtrend", "rsi_d": 24.4},
 "data_gaps": ["long/short ratio"]}
```
```

Reglas:
- Cifras concretas con unidad y fuente cuando no sea obvia.
- Si una herramienta devuelve `status: unavailable` o `error`, se lista en `data_gaps` — **no se inventa el dato**.
- Las etiquetas automáticas de herramientas (p. ej. "BULLISH" por contar osciladores) no se copian sin criterio.

---

## 2. Decisión del portfolio-manager (`decision.md`)

Máximo **~60 líneas**. Estructura fija:

1. `VEREDICTO:` EJECUTAR | ESPERAR | DESCARTAR (una línea, con el porqué en ≤ 20 palabras)
2. Tabla de acciones (ver §3)
3. Rechazados (una línea por activo con el motivo)
4. Kill-switches (lista numerada, condiciones medibles)
5. Riesgo agregado (exposición y pérdida máxima en USDT y % de capital)
6. Persistencia: qué órdenes se guardaron con `place_order` (IDs), qué kill-switches con `add_kill_switch`, qué trades con `record_trade`.

---

## 3. Brief al usuario (respuesta final del coordinador y `brief.md`)

**≤ 25 líneas.** Plantilla:

```markdown
## Veredicto: <ESPERAR — sin entrada a mercado; 3 órdenes límite>

| # | Activo | Libro | Dir | Entrada | SL | TPs | Tamaño | R/R | Caduca |
|---|---|---|---|---|---|---|---|---|---|
| 1 | BTC | Futuros | Long | 63.500 (límite) | 61.600 | 67.500 / 71.200 | 1.500 nocional 3x | 3,1 | 24-nov |

**Por qué** (3-5 viñetas, cada una con un dato):
- Régimen: tendencia bajista fuerte (ADX 40), pero RSI 24 + F&G 10 → rebote probable (base histórica: +X % a 7d en N casos).
- Macro: ...
- Flujos/derivados: ...

**Triggers y kill-switches:** cierre diario < 61.600 → cerrar longs · nada abierto 26–27 nov · ...

**Datos faltantes:** ... (si los hay)
```

Reglas:
- Una sola tabla de acciones. Si no hay acción: `Veredicto: NO HACER NADA` + la condición concreta que cambiaría la decisión.
- Cada orden pendiente debe tener **caducidad** y **condición de cancelación**.
- Sin disclaimers largos: una línea final "Paper trading — no es asesoramiento financiero." basta.
- `--full` (o el usuario pide detalle): añadir debajo un resumen por agente de ≤ 3 líneas cada uno y las rutas de los informes.

---

## 4. Quick (`/quick`)

≤ 6 líneas: precio y cambio 24h, régimen en 3 palabras, funding/OI, F&G, un nivel clave arriba y abajo, y "¿merece `/analyze`? sí/no + motivo".
