export const meta = {
  name: 'alt-scan',
  description: 'Escáner de alts: quant-scanner → risk-specialist → portfolio-manager (persiste órdenes) → learning-agent; devuelve un brief ≤25 líneas',
  whenToUse: 'Cuando el usuario pide buscar oportunidades en altcoins ("/scan", "qué alts mirar", "escanea SOL HYPE ZEC").',
  phases: [
    { title: 'Escanear', detail: 'régimen, breadth, rotación, fuerza relativa, tasas base → máx. 5 setups', model: 'sonnet' },
    { title: 'Riesgo', detail: 'stress test de la shortlist (ATR, correlación, derivados, liquidez)', model: 'sonnet' },
    { title: 'Decidir', detail: 'EJECUTAR / ESPERAR (place_order + add_kill_switch) / DESCARTAR', model: 'opus' },
    { title: 'Aprender', detail: 'registra predicciones de trades y órdenes nuevas', model: 'opus' },
    { title: 'Brief', detail: 'arma el brief en código y lo guarda', model: 'haiku' },
  ],
}

// args opcional: { universe: 'top 40' | 20 | ['SOL','HYPE'], date: 'YYYY-MM-DD' }
const A = args || {}
const UNIVERSE = Array.isArray(A.universe)
  ? A.universe.join(', ')
  : typeof A.universe === 'number'
    ? `los ${A.universe} pares USDT más líquidos de OKX`
    : A.universe || 'los ~40 pares USDT más líquidos de OKX'
const DATE = A.date || null
const DIR = DATE ? `data/reports/${DATE}-ALTS/` : 'data/reports/<AAAA-MM-DD de hoy, UTC>-ALTS/'
const RULES = `Sigue docs/output-format.md. Si una herramienta devuelve status unavailable/error, ponla en data_gaps — nunca inventes el dato. Si la ruta contiene un marcador de fecha, usa la fecha UTC de hoy. NO uses la herramienta Edit.`

// ---------------------------------------------------------------------------
// Esquemas
// ---------------------------------------------------------------------------
const GAPS = { type: 'array', items: { type: 'string' } }
const SETUP = {
  type: 'object',
  required: ['id', 'symbol', 'book', 'side', 'entry', 'sl', 'tp', 'rr'],
  properties: {
    id: { type: 'string' }, // "S1", "S2"...
    symbol: { type: 'string' },
    book: { type: 'string', enum: ['spot', 'futures'] },
    side: { type: 'string', enum: ['long', 'short'] },
    entry: { type: 'number' },
    sl: { type: 'number' },
    tp: { type: 'array', items: { type: 'number' } },
    rr: { type: 'number' },
    sl_atr_multiple: { type: ['number', 'null'] },
    base_rate: { type: ['string', 'null'] },
  },
}

const SCAN_SCHEMA = {
  type: 'object',
  required: ['btc_regime', 'setups', 'rejected', 'data_gaps'],
  properties: {
    btc_regime: { type: 'string' },
    breadth_ma50: { type: ['number', 'null'] },
    setups: { type: 'array', maxItems: 5, items: SETUP },
    rejected: {
      type: 'array',
      items: { type: 'object', required: ['symbol', 'reason'], properties: { symbol: { type: 'string' }, reason: { type: 'string' } } },
    },
    data_gaps: GAPS,
  },
}

const RISK_SCHEMA = {
  type: 'object',
  required: ['verdicts', 'data_gaps'],
  properties: {
    verdicts: {
      type: 'array',
      items: {
        type: 'object',
        required: ['id', 'verdict', 'note'],
        properties: {
          id: { type: 'string' },
          verdict: { type: 'string', enum: ['OK', 'AJUSTAR', 'RECHAZAR'] },
          risk_score: { type: ['number', 'null'] },
          sl_atr_multiple: { type: ['number', 'null'] },
          note: { type: 'string' }, // qué cambiar o por qué se rechaza (≤15 palabras)
        },
      },
    },
    data_gaps: GAPS,
  },
}

const DECISION_SCHEMA = {
  type: 'object',
  required: ['verdict', 'headline', 'actions', 'rejected', 'kill_switches', 'persisted'],
  properties: {
    verdict: { type: 'string', enum: ['EJECUTAR', 'ESPERAR', 'DESCARTAR', 'NO HACER NADA'] },
    headline: { type: 'string' }, // ≤20 palabras
    actions: {
      type: 'array',
      items: {
        type: 'object',
        required: ['symbol', 'book', 'side', 'entry', 'sl', 'tps', 'size', 'rr', 'expires'],
        properties: {
          symbol: { type: 'string' },
          book: { type: 'string' },
          side: { type: 'string' },
          entry: { type: 'string' }, // "63,50 (límite)" o "mercado 69,4"
          sl: { type: 'string' },
          tps: { type: 'string' },
          size: { type: 'string' }, // "800 nocional 2x"
          rr: { type: 'number' },
          expires: { type: 'string' },
          id: { type: 'string' }, // order_id o trade_id persistido
        },
      },
    },
    reasons: { type: 'array', items: { type: 'string' } }, // 3-5 viñetas, cada una con un dato
    rejected: { type: 'array', items: { type: 'string' } }, // "SOL: duplica beta de BTC (corr 0,88)"
    kill_switches: { type: 'array', items: { type: 'string' } },
    persisted: {
      type: 'object',
      properties: {
        orders: { type: 'array', items: { type: 'string' } },
        trades: { type: 'array', items: { type: 'string' } },
        kill_switches: { type: 'array', items: { type: 'string' } },
        predictions: { type: 'array', items: { type: 'string' } },
      },
    },
    data_gaps: GAPS,
  },
}

const LEARN_SCHEMA = {
  type: 'object',
  required: ['prediction_ids'],
  properties: { prediction_ids: { type: 'array', items: { type: 'string' } } },
}

// ---------------------------------------------------------------------------
// Fase 1 — Escanear
// ---------------------------------------------------------------------------
phase('Escanear')
const scan = await agent(
  `Eres el agente quant-scanner del Crypto Trading Desk. Lee agents/quant-scanner.md para tu marco.
   Universo: ${UNIVERSE}.
   Usa las herramientas MCP: crypto-quant (classify_regime, market_breadth, sector_rotation, relative_strength_scan,
   volatility_regime, behavior_base_rates, correlation_matrix, seasonality), crypto-technical (get_support_resistance),
   crypto-futures (get_funding_rate con exchange='okx') o crypto-derivatives (get_hyperliquid_market), crypto-intel (get_token_unlocks).
   Máximo 5 setups COMPLETOS (entrada límite en un nivel, SL estructural y ≥ 1×ATR diario, TPs, R/R ≥ 3 spot / ≥ 2 futuros,
   tasa base). Ids S1..S5. Rechazos en una línea con motivo.
   Escribe ${DIR}alt-scan.md (§1: ≤40 líneas + JSON). ${RULES}`,
  { label: 'quant-scanner', phase: 'Escanear', model: 'sonnet', schema: SCAN_SCHEMA },
)

if (!scan || !scan.setups || !scan.setups.length) {
  const why = scan ? (scan.rejected || []).slice(0, 4).map((r) => `${r.symbol}: ${r.reason}`).join(' · ') : 'el escáner no respondió'
  const brief = `## Veredicto: NO HACER NADA — ningún setup de alts pasa las reglas\n\nRégimen BTC: ${(scan && scan.btc_regime) || 'n/d'}\n**Descartados:** ${why || 'n/d'}\n**Qué lo cambiaría:** breadth > 30% sobre MA50 o un líder sectorial que vuelva a su nivel con SL ≥ 1×ATR.\n_Paper trading — no es asesoramiento financiero._`
  log('Sin setups: se omiten riesgo, decisión y aprendizaje')
  return brief
}
log(`${scan.setups.length} setups en la shortlist: ${scan.setups.map((s) => s.symbol).join(', ')}`)

// ---------------------------------------------------------------------------
// Fase 2 — Riesgo (un solo agente: necesita la correlación entre todos los setups)
// ---------------------------------------------------------------------------
phase('Riesgo')
const risk = await agent(
  `Eres el agente risk-specialist. Lee agents/risk-specialist.md para tu marco.
   PRIMERO lee ${DIR}alt-scan.md (ya escrito). Setups a evaluar:
   ${JSON.stringify(scan.setups)}
   Usa las herramientas MCP: crypto-quant (volatility_regime, correlation_matrix con BTC y las posiciones abiertas de
   get_portfolio_state), crypto-derivatives (get_liquidations_history, get_aggregated_oi), crypto-market-microstructure
   (analyze_orderbook_depth, calculate_market_impact), crypto-flows (get_stablecoin_flows).
   Marca cualquier SL < 1×ATR diario. Veredicto OK / AJUSTAR / RECHAZAR por id.
   Escribe ${DIR}risk-assessment.md (§1). ${RULES}`,
  { label: 'risk-specialist', phase: 'Riesgo', model: 'sonnet', schema: RISK_SCHEMA },
)

// ---------------------------------------------------------------------------
// Fase 3 — Decidir y persistir
// ---------------------------------------------------------------------------
phase('Decidir')
const decision = await agent(
  `Eres el agente portfolio-manager. Lee agents/portfolio-manager.md para tu marco (reglas de los dos libros).
   PRIMERO lee ${DIR}alt-scan.md y ${DIR}risk-assessment.md.
   Veredictos de riesgo: ${JSON.stringify((risk && risk.verdicts) || 'no disponible — sé más conservador')}
   Pasos con herramientas MCP:
   1. crypto-learning-db: get_portfolio_state(), list_orders(status='pending'), list_kill_switches(). Las órdenes pendientes
      cuentan como ejecutadas para los límites; alts con corr > 0,8 son una sola apuesta.
   2. Verifica precios con get_exchange_prices (crypto-exchange).
   3. Por setup superviviente: EJECUTAR (record_trade) sólo si el precio está en la entrada ahora; si no,
      place_order(symbol, portfolio_type, side, usd_amount, order_type='limit', trigger_price, condition_text, stop_loss,
      take_profits='[{"price": .., "pct": 50}, ..]', leverage, expires_at ISO UTC, reasoning, learning, source_report='${DIR}')
      y add_kill_switch(rule_text, metric, operator, threshold, action_text, scope) por cada invalidación medible
      (métricas válidas: btc_price, btc_daily_close, btc_weekly_close, btc_funding, fear_greed, futures_realized_loss, <SYMBOL>_price).
   4. record_prediction(trade_id=None, source='scan') para la predicción principal de los setups descartados relevantes.
   5. Escribe ${DIR}decision.md (§2, ≤60 líneas).
   Devuelve la decisión estructurada con TODOS los IDs persistidos. ${RULES}`,
  { label: 'portfolio-manager', phase: 'Decidir', model: 'opus', schema: DECISION_SCHEMA },
)

if (!decision) {
  return `## Veredicto: sin decisión — el portfolio-manager no respondió\nShortlist en ${DIR}alt-scan.md y riesgo en ${DIR}risk-assessment.md. Reintenta con \`/scan\`.`
}

// ---------------------------------------------------------------------------
// Fase 4 — Aprender (sólo si se abrió algo o se dejaron órdenes)
// ---------------------------------------------------------------------------
const P = decision.persisted || {}
const newStuff = [...(P.orders || []), ...(P.trades || [])]
let learnedIds = []
if (newStuff.length) {
  phase('Aprender')
  const learn = await agent(
    `Eres el learning-agent. Lee agents/learning-agent.md (Mission 2).
     Lee ${DIR}decision.md. Registra predicciones comprobables con record_prediction() de crypto-learning-db para:
     trades nuevos ${JSON.stringify(P.trades || [])} (source='trade', con trade_id) y órdenes nuevas ${JSON.stringify(P.orders || [])}
     (trade_id=None, source='order', order_id: ¿se ejecuta antes de caducar? ¿TP1 antes que SL?).
     Antes consulta query_predictions para no duplicar las que ya registró el portfolio-manager. ${RULES}`,
    { label: 'learning-agent', phase: 'Aprender', model: 'opus', schema: LEARN_SCHEMA },
  )
  learnedIds = (learn && learn.prediction_ids) || []
} else {
  log('Sin órdenes ni trades nuevos: se omite el registro de predicciones')
}

// ---------------------------------------------------------------------------
// Fase 5 — Brief (armado en código, ≤25 líneas)
// ---------------------------------------------------------------------------
phase('Brief')
const L = []
L.push(`## Veredicto: ${decision.verdict} — ${decision.headline}`)
L.push('')
if (decision.actions && decision.actions.length) {
  L.push('| # | Activo | Libro | Dir | Entrada | SL | TPs | Tamaño | R/R | Caduca |')
  L.push('|---|---|---|---|---|---|---|---|---|---|')
  decision.actions.slice(0, 5).forEach((a, i) =>
    L.push(`| ${a.id || i + 1} | ${a.symbol} | ${a.book} | ${a.side} | ${a.entry} | ${a.sl} | ${a.tps} | ${a.size} | ${Number(a.rr).toFixed(1)} | ${a.expires} |`),
  )
  L.push('')
}
const reasons = (decision.reasons || []).slice(0, 4)
if (reasons.length) {
  L.push('**Por qué:**')
  reasons.forEach((r) => L.push(`- ${r}`))
}
if ((decision.rejected || []).length) L.push(`**Descartados:** ${decision.rejected.slice(0, 5).join(' · ')}`)
if ((decision.kill_switches || []).length) L.push(`**Kill-switches:** ${decision.kill_switches.slice(0, 4).join(' · ')}`)
const gaps = [...new Set([scan, risk, decision].filter(Boolean).flatMap((r) => r.data_gaps || []))]
if (!risk) gaps.unshift('risk-specialist sin respuesta')
if (gaps.length) L.push(`**Datos faltantes:** ${gaps.slice(0, 6).join(', ')}`)
const ids = [
  ...(P.orders || []).map((x) => `orden ${x}`),
  ...(P.trades || []).map((x) => `trade ${x}`),
  ...(P.kill_switches || []).map((x) => `ks ${x}`),
].join(', ')
L.push(`Guardado: ${ids || 'nada'} · predicciones: ${[...(P.predictions || []), ...learnedIds].length} · informes en ${DIR}`)
L.push('_Paper trading — no es asesoramiento financiero._')
const brief = L.join('\n')

await agent(
  `Guarda EXACTAMENTE este markdown (sin cambiar nada salvo el marcador de fecha de la ruta, si lo hay) con la herramienta
   Write en ${DIR}brief.md. Responde sólo con la ruta escrita. NO uses la herramienta Edit.

${brief}`,
  { label: 'write-brief', phase: 'Brief', model: 'haiku' },
)

return brief
