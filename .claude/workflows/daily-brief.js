export const meta = {
  name: 'daily-brief',
  description: 'Brief diario (≤25 líneas): régimen, BTC, viento macro/flujos, top 3 oportunidades, órdenes pendientes y calendario 7d',
  whenToUse: 'Cada mañana o cuando el usuario pide "brief", "cómo está el mercado hoy" o un resumen diario. No toma decisiones ni abre trades.',
  phases: [
    { title: 'Recolectar', detail: 'market-monitor (haiku) + macro-flows (sonnet) + quant-scanner (sonnet) en paralelo' },
    { title: 'Brief', detail: 'arma el brief en código y lo guarda en brief.md', model: 'haiku' },
  ],
}

// args opcional: { date: 'YYYY-MM-DD' } — el script no puede leer el reloj (resume determinista).
const DATE = (args && args.date) || null
const DIR = DATE ? `data/reports/${DATE}-BRIEF/` : 'data/reports/<AAAA-MM-DD de hoy, UTC>-BRIEF/'
const RULES = `Sigue docs/output-format.md §1 (≤40 líneas + bloque JSON). Si una herramienta devuelve status unavailable/error, ponla en data_gaps — nunca inventes el dato. NO uses la herramienta Edit.`

// ---------------------------------------------------------------------------
// Esquemas
// ---------------------------------------------------------------------------
const GAPS = { type: 'array', items: { type: 'string' } }

const MARKET_SCHEMA = {
  type: 'object',
  required: ['btc', 'fear_greed', 'pending_orders', 'data_gaps'],
  properties: {
    btc: {
      type: 'object',
      required: ['price', 'chg_24h'],
      properties: {
        price: { type: 'number' },
        chg_24h: { type: 'number' },
        funding: { type: ['number', 'null'] },
        oi_chg_24h: { type: ['number', 'null'] },
        liq_bias: { type: ['string', 'null'] },
        coinbase_premium: { type: ['number', 'null'] },
      },
    },
    eth: { type: 'object', properties: { price: { type: 'number' }, chg_24h: { type: 'number' } } },
    fear_greed: { type: ['number', 'null'] },
    pending_orders: {
      type: 'array',
      items: {
        type: 'object',
        required: ['id', 'symbol', 'trigger', 'distance_pct'],
        properties: {
          id: { type: 'string' },
          symbol: { type: 'string' },
          side: { type: 'string' },
          trigger: { type: 'number' },
          distance_pct: { type: 'number' },
          expires: { type: 'string' },
        },
      },
    },
    data_gaps: GAPS,
  },
}

const VERDICT = {
  type: 'object',
  required: ['call', 'strength', 'driver'],
  properties: {
    call: { type: 'string', enum: ['A FAVOR', 'NEUTRAL', 'EN CONTRA'] },
    strength: { type: 'number' },
    driver: { type: 'string' }, // ≤10 palabras con un dato
  },
}

const MACRO_SCHEMA = {
  type: 'object',
  required: ['verdict_1_7d', 'verdict_2_6w', 'events_7d', 'data_gaps'],
  properties: {
    verdict_1_7d: VERDICT,
    verdict_2_6w: VERDICT,
    events_7d: {
      type: 'array',
      items: { type: 'object', required: ['date', 'event'], properties: { date: { type: 'string' }, event: { type: 'string' } } },
    },
    data_gaps: GAPS,
  },
}

const SCAN_SCHEMA = {
  type: 'object',
  required: ['btc_regime', 'setups', 'data_gaps'],
  properties: {
    btc_regime: { type: 'string' },
    breadth_ma50: { type: ['number', 'null'] },
    top_sectors: { type: 'array', items: { type: 'string' } },
    setups: {
      type: 'array',
      maxItems: 3,
      items: {
        type: 'object',
        required: ['symbol', 'book', 'side', 'entry', 'sl', 'tp', 'rr', 'base_rate', 'entry_distance_pct'],
        properties: {
          symbol: { type: 'string' },
          book: { type: 'string', enum: ['Spot', 'Futuros'] },
          side: { type: 'string', enum: ['Long', 'Short'] },
          entry: { type: 'string' }, // p. ej. "63.500 (límite)"
          sl: { type: 'string' },
          tp: { type: 'string' }, // "67.500 / 71.200"
          rr: { type: 'number' },
          base_rate: { type: 'string' }, // "62% a 7d, N=21"
          entry_distance_pct: { type: 'number' }, // distancia del precio actual a la entrada
        },
      },
    },
    data_gaps: GAPS,
  },
}

// ---------------------------------------------------------------------------
// Fase 1 — Tres agentes en paralelo (barrera: el brief necesita los tres)
// ---------------------------------------------------------------------------
phase('Recolectar')
const [market, macro, scan] = await parallel([
  () =>
    agent(
      `Eres el agente market-monitor del Crypto Trading Desk. Lee agents/market-monitor.md para tu marco.
       Snapshot de BTC y ETH llamando a las herramientas MCP en UN solo lote paralelo:
       get_exchange_prices (crypto-exchange), get_funding_rate(exchange='okx') (crypto-futures), get_aggregated_oi y
       get_liquidations_history (crypto-derivatives), get_coinbase_premium (crypto-flows), get_fear_greed_index (crypto-data).
       Además: list_orders(status='pending') de crypto-learning-db y get_exchange_prices de cada símbolo con orden;
       calcula la distancia % del precio actual al disparo de cada orden.
       Unidades: funding en % por 8h (0,01 = 0,01 %), oi_chg_24h y coinbase_premium en %.
       Escribe ${DIR}market-data.md. ${RULES}
       Devuelve los datos estructurados.`,
      { label: 'market-monitor', phase: 'Recolectar', model: 'haiku', schema: MARKET_SCHEMA },
    ),
  () =>
    agent(
      `Eres el agente macro-flows. Lee agents/macro-flows.md para tu marco.
       Llama a las herramientas MCP: crypto-macro (get_macro_snapshot, get_crypto_macro_correlation, get_fed_odds,
       get_macro_calendar de los próximos 7 días), crypto-flows (get_etf_flows, get_stablecoin_flows, get_coinbase_premium),
       crypto-derivatives (get_options_snapshot, get_max_pain), crypto-onchain (get_btc_valuation).
       Da el veredicto A FAVOR / NEUTRAL / EN CONTRA para 1-7d y 2-6w, cada uno con su dato principal (≤10 palabras).
       Escribe ${DIR}macro-flows.md. ${RULES}
       Devuelve los datos estructurados.`,
      { label: 'macro-flows', phase: 'Recolectar', model: 'sonnet', schema: MACRO_SCHEMA },
    ),
  () =>
    agent(
      `Eres el agente quant-scanner. Lee agents/quant-scanner.md para tu marco.
       Escaneo diario con las herramientas MCP de crypto-quant: classify_regime('BTC'), market_breadth, sector_rotation,
       relative_strength_scan sobre los ~40 pares USDT más líquidos de OKX; para los candidatos volatility_regime,
       behavior_base_rates y correlation_matrix; unlocks con get_token_unlocks (crypto-intel).
       Devuelve como máximo las 3 MEJORES oportunidades COMPLETAS (libro, lado, entrada límite en un nivel, SL ≥ 1×ATR,
       TPs, R/R ≥ 3 spot / ≥ 2 futuros, tasa base). Si ninguna califica, devuelve setups vacío.
       Escribe ${DIR}alt-scan.md. ${RULES}
       Devuelve los datos estructurados.`,
      { label: 'quant-scanner', phase: 'Recolectar', model: 'sonnet', schema: SCAN_SCHEMA },
    ),
])

// ---------------------------------------------------------------------------
// Fase 2 — Armar el brief en código (≤25 líneas garantizadas) y guardarlo
// ---------------------------------------------------------------------------
phase('Brief')
const n = (x, d = 0) => (x === null || x === undefined || Number.isNaN(x) ? 'n/d' : Number(x).toFixed(d))
const pct = (x, d = 2) => (x === null || x === undefined ? 'n/d' : `${x > 0 ? '+' : ''}${Number(x).toFixed(d)}%`)

const lines = []
const regime = (scan && scan.btc_regime) || 'n/d'
const breadth = scan && scan.breadth_ma50 != null ? ` · Breadth ${n(scan.breadth_ma50)}% > MA50` : ''
lines.push(`## Brief ${DATE || ''} — Régimen: ${regime}${breadth}`)

if (market && market.btc) {
  const b = market.btc
  lines.push(
    `**BTC ${n(b.price)}** (${pct(b.chg_24h)}) · funding ${pct(b.funding, 4)} · OI ${pct(b.oi_chg_24h, 1)} · ` +
      `F&G ${n(market.fear_greed)} · Coinbase premium ${pct(b.coinbase_premium, 3)}`,
  )
} else {
  lines.push('**BTC:** n/d (market-monitor falló)')
}

if (macro) {
  const v = (x) => `${x.call} (${x.driver})`
  lines.push(`**Viento:** 1-7d ${v(macro.verdict_1_7d)} · 2-6w ${v(macro.verdict_2_6w)}`)
} else {
  lines.push('**Viento:** n/d (macro-flows falló)')
}
lines.push('')

const setups = (scan && scan.setups) || []
if (setups.length) {
  lines.push('| # | Activo | Libro | Dir | Entrada | SL | TPs | R/R | Base rate |')
  lines.push('|---|---|---|---|---|---|---|---|---|')
  setups.slice(0, 3).forEach((s, i) =>
    lines.push(`| ${i + 1} | ${s.symbol} | ${s.book} | ${s.side} | ${s.entry} | ${s.sl} | ${s.tp} | ${n(s.rr, 1)} | ${s.base_rate} |`),
  )
} else {
  lines.push('Sin setups que pasen las reglas hoy.')
}
lines.push('')

const orders = (market && market.pending_orders) || []
lines.push(
  `**Órdenes pendientes:** ` +
    (orders.length
      ? orders.slice(0, 5).map((o) => `${o.id} ${o.symbol} ${n(o.trigger)} a ${pct(o.distance_pct, 1)}${o.expires ? ` · caduca ${o.expires}` : ''}`).join(' · ')
      : 'ninguna'),
)
const events = (macro && macro.events_7d) || []
lines.push(`**Calendario 7d:** ` + (events.length ? events.slice(0, 6).map((e) => `${e.date} ${e.event}`).join(' · ') : 'n/d'))

// Acción: sugerir /analyze sólo si hay algo realmente cerca
const near = setups.filter((s) => Math.abs(s.entry_distance_pct) <= 3)
const nearOrder = orders.filter((o) => Math.abs(o.distance_pct) <= 1)
if (near.length) {
  lines.push(`**Acción:** \`/analyze ${near[0].symbol}\` — entrada a ${pct(near[0].entry_distance_pct, 1)} del precio, R/R ${n(near[0].rr, 1)}`)
} else if (nearOrder.length) {
  lines.push(`**Acción:** \`/monitor\` — orden ${nearOrder[0].id} a ${pct(nearOrder[0].distance_pct, 1)} de su disparo`)
} else {
  lines.push('**Acción:** nada que hacer hoy.')
}

const gaps = [market, macro, scan].filter(Boolean).flatMap((r) => r.data_gaps || [])
const failed = [['market-monitor', market], ['macro-flows', macro], ['quant-scanner', scan]].filter(([, r]) => !r).map(([k]) => k)
const allGaps = [...new Set([...failed.map((k) => `${k} sin respuesta`), ...gaps])]
if (allGaps.length) lines.push(`**Datos faltantes:** ${allGaps.slice(0, 6).join(', ')}`)
lines.push('_Paper trading — no es asesoramiento financiero._')

const brief = lines.join('\n')
log(`Brief: ${lines.length} líneas · ${setups.length} setups · ${orders.length} órdenes pendientes`)

await agent(
  `Guarda EXACTAMENTE este markdown (sin cambiar nada) con la herramienta Write en ${DIR}brief.md.
   Si la ruta contiene un marcador de fecha, sustitúyelo por la fecha UTC de hoy (AAAA-MM-DD).
   Responde sólo con la ruta escrita. NO uses la herramienta Edit.

${brief}`,
  { label: 'write-brief', phase: 'Brief', model: 'haiku' },
)

return brief
