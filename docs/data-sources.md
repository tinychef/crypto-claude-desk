# Fuentes de datos V2 — qué conecta el desk, cómo activarlas y cuánto cuestan

Precios verificados el **2026-11-17** en las páginas oficiales o en agregadores (indicado). Cambian a menudo: revísalos antes de suscribirte.

## Cómo se activan las fuentes con key

| Entorno | Método |
|---|---|
| Local (Claude Code en tu PC) | Variable de entorno `DESK_<PROVEEDOR>_KEY` (p. ej. `DESK_COINGLASS_KEY`). También acepta `<PROVEEDOR>_API_KEY`. |
| Nube (claude.ai/code) | **Network secret** del entorno: el proxy inyecta la key en el header del host. Solo funciona con proveedores que aceptan la key **por header** (columna "Header"). Las que van por query param (CryptoPanic, Etherscan) solo funcionan en local con variable de entorno. |
| Sin key | La herramienta responde `{"status": "unavailable", ...}` y los agentes lo tratan como dato faltante. Nunca se inventan datos. |

Además, en la nube cada host tiene que estar en el **allowlist de red** del entorno (Settings → entorno → Network). Los hosts necesarios están en la última sección.

## Gratis (sin key o con key gratuita)

| Proveedor | Servidor MCP | Datos | Key | Header | Coste |
|---|---|---|---|---|---|
| OKX / Kraken / KuCoin / MEXC / Coinbase (públicas) | exchange, technical, futures, advanced, microstructure, quant | precios, OHLCV, orderbook, funding, OI, L/S (OKX Rubik) | no | — | $0 |
| alternative.me | data | Fear & Greed | no | — | $0 |
| CoinGecko **Demo** | data, quant | metadata, categorías/sectores, rankings | gratis (`DESK_COINGECKO_KEY`) | `x-cg-demo-api-key` | $0 (10k llamadas/mes) |
| DefiLlama | defillama, flows, intel | TVL, stablecoins, DEX, emisiones | no | — | $0 |
| Polymarket gamma | polymarket, macro | probabilidades de eventos (Fed, precios) | no | — | $0 |
| Kalshi (lectura pública) | macro | probabilidades Fed | no | — | $0 |
| **Coinalyze** | derivatives | OI, funding y **liquidaciones agregadas** entre exchanges | gratis (`DESK_COINALYZE_KEY`) | `api_key` | $0 (citar la fuente) |
| Deribit público | derivatives | opciones: DVOL, put/call, skew, max pain, basis | no | — | $0 |
| Hyperliquid info API | derivatives | funding/OI de perps on-chain | no | — | $0 |
| FRED (CSV público) / Stooq | macro | DXY, 2Y/10Y, tipos reales, M2, Fed funds, SPX, NDX, oro | no | — | $0 |
| Farside | flows | flujos diarios de ETF BTC/ETH/SOL | no (scrape) | — | $0 |
| Coinbase / Upbit | flows | Coinbase premium, Kimchi premium | no | — | $0 |
| CoinMetrics Community | onchain | MVRV, direcciones activas, NVT | no | — | $0 |
| mempool.space | onchain | fees, hashrate, dificultad | no | — | $0 |
| RSS (CoinDesk, Cointelegraph, Decrypt, The Block) / Reddit JSON | intel | titulares y pulso social | no | — | $0 |
| CryptoPanic | intel | noticias con votos de sentimiento | gratis (`DESK_CRYPTOPANIC_KEY`) | ✗ (query param, solo local) | $0 |

## De pago (opcionales; se activan solo si hay key)

| Proveedor | Servidor MCP | Qué añade | Plan con API | Coste/mes | Recomendación |
|---|---|---|---|---|---|
| **Coinglass** | derivatives, intel | heatmap y mapa de liquidaciones, L/S por exchange, ETF | Hobbyist (uso personal) | **$29** (Startup $79, Standard $299 comercial) | **1.ª compra**: el mejor valor para trading táctico |
| **CryptoQuant** | onchain | netflows de exchanges, reservas, whales, SOPR | Professional | **$99** (Advanced $29–39 solo mercado; Premium $799) | 2.ª compra si quieres flujos on-chain |
| CoinGecko de pago | data, quant | más límite, endpoints pro | Basic / Analyst | $35 / $129 | solo si el Demo se queda corto |
| LunarCrush | intel | Galaxy Score, dominancia social | pay-as-you-go o Light | $1 por 1k peticiones / $90 | opcional |
| Santiment | intel | social y on-chain de devs | Pro / Max | ~$49 / ~$249 (terceros) | opcional |
| Nansen | (futuro) | smart money, etiquetas de wallets | Pro + créditos | $69 ($49 anual) + $10 por 10k créditos | opcional, para memecoins y alts on-chain |
| Tokenomist | intel | calendario de desbloqueos detallado | API | bajo cotización | DefiLlama cubre lo básico gratis |
| Glassnode | onchain | on-chain profundo (SOPR, LTH/STH, etc.) | Professional + API add-on | ~$600–1.000 (bajo cotización) | solo con presupuesto alto |
| Kaiko / Amberdata | — | datos institucionales tick-level | enterprise | > $1.000 | no recomendado para este desk |

**Ruta recomendada:**
1. Todo lo gratis, más las keys gratuitas de CoinGecko Demo y Coinalyze. Cubre ~80 % del valor.
2. **+ Coinglass Hobbyist ($29)**.
3. **+ CryptoQuant Professional ($99)** si las decisiones dependen de flujos de exchanges.
4. Glassnode solo si haces análisis on-chain de ciclo con frecuencia.

## Coste de Claude por corrida (orientativo)
- Corrida V1 `/analyze BTC` + escaneo de alts (2026-11-16): ~1,1 M tokens de subagentes.
- Objetivo V2: `/brief` ≤ 300 k y `/analyze` ≤ 900 k, gracias a informes acotados (docs/output-format.md) y a usar haiku en las tareas de datos.

## Allowlist de red para sesiones cloud
Hosts por servidor (añádelos en la configuración de red del entorno):
- **Núcleo (ya permitidos en el entorno `crypto-claude-desk`):** `www.okx.com`, `api.kraken.com`, `api.kucoin.com`, `api.mexc.com`, `api.coingecko.com`, `api.alternative.me`, `api.llama.fi`, `stablecoins.llama.fi`, `gamma-api.polymarket.com`
- **derivatives:** `api.coinalyze.net`, `www.deribit.com`, `api.hyperliquid.xyz`
- **macro:** `fred.stlouisfed.org`, `stooq.com`, `api.elections.kalshi.com`
- **flows:** `farside.co.uk`, `api.exchange.coinbase.com`, `api.upbit.com`, `api.frankfurter.app`
- **onchain:** `community-api.coinmetrics.io`, `mempool.space` (pago: `api.cryptoquant.com`, `api.glassnode.com`)
- **intel:** `www.coindesk.com`, `cointelegraph.com`, `decrypt.co`, `www.theblock.co`, `www.reddit.com`, `cryptopanic.com` (pago: `open-api-v4.coinglass.com`, `lunarcrush.com`)

Binance (`api.binance.com`, `fapi.binance.com`) y Bybit bloquean IPs de EE. UU. (HTTP 451/403). La V2 usa OKX por defecto; se puede cambiar el orden con `DESK_EXCHANGES` / `DESK_FUTURES_EXCHANGES`.

## Fuentes de los precios
- [CoinGlass pricing](https://coinglass.com/pricing)
- [CryptoQuant pricing](https://cryptoquant.com/pricing)
- [Glassnode pricing](https://studio.glassnode.com/pricing?billingCycle=yearly) · [CostBench Glassnode API](https://www.costbench.com/software/blockchain-data-api/glassnode-api/)
- [CoinGecko API pricing](https://coingecko.com/api/pricing)
- [LunarCrush cost](https://lunarcrush.com/support/how-much-does-lunarcrush-cost)
- [Santiment API plans](https://academy.santiment.net/products-and-plans/sanapi-plans) · [CostBench Santiment](https://costbench.com/software/onchain-analytics/santiment/)
- [Tokenomist pricing](https://www.tokenomist.ai/pricing)
- [Nansen credits & pricing](https://docs.nansen.ai/getting-started/credits)
- [Coinalyze API docs](https://api.coinalyze.net/v1/doc)
