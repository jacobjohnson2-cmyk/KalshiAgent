# Kalshi Agent

A terminal chat agent, powered by Claude, that manages your Kalshi positions. It reads your balance, positions, orders, fills and market data, and it can **place and cancel limit orders by itself**. Hard risk limits enforced in code sit between the model and your account. The model can't place an order that breaks them.

```
you › what's my exposure, and trim anything closing this week by half
→ get_portfolio_summary
→ get_orderbook {"ticker": "KXFED-25DEC-H0"}
→ place_order {"ticker": "KXFED-25DEC-H0", "side": "yes", "action": "sell", "count": 10, ...}
● PLACED SELL 10 YES KXFED-25DEC-H0 @ 61c → resting (id 7f3c…)
```

## How it works

```
CLI chat ──► Agent (Claude, streaming tool-use loop)
                 │ tools
                 ▼
            RiskGuard ──(approved)──► KalshiClient ──► Kalshi Trade API v2
                 │                        (RSA-PSS signed)
                 └──► logs/audit.jsonl
```

| File | Role |
|---|---|
| `src/kalshi_agent/kalshi_client.py` | Signed REST client. Reads either the legacy cent fields or the newer `*_dollars` / `*_fp` fields and converts both to integer cents. |
| `src/kalshi_agent/risk.py` | `RiskGuard`: pure, deterministic checks on every order. |
| `src/kalshi_agent/tools.py` | The tools Claude can call. `place_order` fetches fresh account and market state, runs the guard, writes to the audit log, then sends the order. |
| `src/kalshi_agent/agent.py` | System prompt and the streaming tool loop. Handles refusal fallback, `max_tokens` truncation and rollback on Ctrl-C. |
| `src/kalshi_agent/cli.py` | Chat REPL, slash commands and startup safety checks. |

## Setup

1. **Python 3.10+**
   ```bash
   python -m venv .venv && source .venv/bin/activate
   pip install -e ".[dev]"
   ```
2. **Kalshi API key.** In Kalshi (use the **demo** site first, demo.kalshi.co), open Account → API Keys and create a key. Save the downloaded private key as `kalshi_private_key.pem` in this directory. It is gitignored.
3. **Anthropic API key.** Get one from console.anthropic.com.
4. **Configure:** `cp .env.example .env`, then fill in `ANTHROPIC_API_KEY` and `KALSHI_API_KEY_ID`.
5. **Run:**
   ```bash
   python -m kalshi_agent        # or: kalshi-agent
   ```

It starts on the **demo** environment with **`DRY_RUN=true`**. In dry-run mode every order goes through the risk guard and is logged, but nothing is sent to Kalshi. Once you're happy with how it behaves, set `DRY_RUN=false`, or type `/dryrun off` during a session.

To use production, set `KALSHI_ENV=prod`. You must also type `CONFIRM PROD` at startup.

## Risk limits

Set these in `.env`. Money values are in cents.

| Setting | Default | Meaning |
|---|---|---|
| `MAX_ORDER_COST_CENTS` | 2500 ($25) | Maximum cost of a single buy order (count × price) |
| `MAX_POSITION_CONTRACTS_PER_MARKET` | 100 | Maximum contracts held in one market after the order fills |
| `MAX_TOTAL_EXPOSURE_CENTS` | 20000 ($200) | Cost basis of all positions + resting buy orders + this order |
| `MAX_ORDERS_PER_SESSION` | 20 | Risk-increasing orders allowed per session |
| `MAX_SESSION_LOSS_CENTS` | 5000 ($50) | Once account value falls this far below its value at session start, only risk-reducing orders are allowed |
| `MAX_SLIPPAGE_CENTS` | 3 | Maximum distance of a limit price past the best ask (for buys) or below the best bid (for sells) |
| `ALLOWED_TICKER_PREFIXES` | *(all)* | Optional comma-separated allowlist, e.g. `KXFED,KXCPI` |
| `BLOCKED_TICKER_PREFIXES` | *(none)* | Optional comma-separated blocklist |
| `KILL_SWITCH` | false | Start with trading halted |

The guard also enforces these rules:
- Limit orders only, priced 1–99¢, and only in markets whose status is open or active.
- No naked or flipping sells: you can only sell as many contracts of a side as you hold.
- **Risk-reducing orders are always allowed** past the size, exposure, allowlist, session-count and loss limits. These are orders that move a position toward zero without flipping it, such as selling what you hold or buying the opposite side up to your held size. The slippage limit still applies to them.
- The guard reads positions, orders, balance and the order book fresh from Kalshi before every check. It never uses numbers supplied by the model.
- `/halt` blocks every new order. Cancels still go through.

Every order check, placement, cancel and error is written to `logs/audit.jsonl`, together with the reason the model gave for it.

## Commands

| Command | |
|---|---|
| `/positions` | Positions table with mark-to-bid and estimated P&L |
| `/orders` | Resting orders |
| `/limits` | Limits, session usage, dry-run/halt state |
| `/halt`, `/resume` | Stop / restart order placement |
| `/dryrun on\|off` | Toggle dry-run |
| `/quit` | Exit |

## Model

The default model is `claude-opus-5` with adaptive thinking and `effort=high`. You can change them with `KALSHI_AGENT_MODEL` and `KALSHI_AGENT_EFFORT`. Requests opt into Anthropic's server-side refusal fallback (`fallbacks: "default"`), so if the model declines a request, the API retries it on another model instead of stopping.

## Development

```bash
pytest          # signing, risk guard, tools (mocked Kalshi), agent loop (mocked Claude)
ruff check .
mypy src
```

The tests make no network calls. The Kalshi API docs were not reachable from the environment this was built in, so field names follow the Trade API v2 as of 2025–26. Before live trading, check your first demo session against the Kalshi docs, especially order creation, which sends `yes_price` / `no_price` in cents.

## Not included yet

Possible follow-ups:
- A scheduled `review` mode with alerts
- A WebSocket price feed
- A daily loss limit that persists across sessions (the current limit resets each session)
- Backtesting
