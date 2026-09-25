"""Interactive terminal chat for the Kalshi agent."""

from __future__ import annotations

import json
import sys
from typing import Any

import anthropic
from rich.console import Console
from rich.table import Table

from .agent import Agent
from .audit import AuditLog
from .config import Settings
from .kalshi_client import KalshiClient, KalshiError, load_private_key
from .risk import RiskGuard, RiskLimits
from .tools import NoInput, Toolbox

console = Console()

TRADE_STYLES = {"placed": "bold green", "cancelled": "bold yellow", "dry_run": "bold cyan",
                "rejected": "bold red", "error": "bold red"}

HELP = """[bold]Commands[/bold]
  /positions      show positions
  /orders         show resting orders
  /limits         show risk limits and session usage
  /halt           block all new orders (cancels still allowed)
  /resume         lift /halt
  /dryrun on|off  toggle dry-run
  /help           this help
  /quit           exit"""


def _money(c: int | None) -> str:
    return "—" if c is None else f"${c / 100:,.2f}"


def _on_trade_event(kind: str, msg: str) -> None:
    console.print(f"[{TRADE_STYLES.get(kind, 'bold')}]● {kind.upper()}[/] {msg}")


def _show_positions(toolbox: Toolbox) -> None:
    s = toolbox.get_portfolio_summary(NoInput())
    console.print(f"Cash {_money(s['cash_balance_cents'])} · "
                  f"Account value {_money(s['account_value_cents'])}")
    table = Table("Ticker", "Side", "Qty", "Cost", "Bid", "Mark", "Unreal. P&L", "Closes")
    for p in s["positions"]:
        bid = p.get("yes_bid") if p["held_side"] == "yes" else p.get("no_bid")
        table.add_row(p["ticker"], (p["held_side"] or "-").upper(), str(p["contracts"]),
                      _money(p["cost_basis_cents"]), "—" if bid is None else f"{bid}c",
                      _money(p.get("marked_value_cents")),
                      _money(p.get("unrealized_pnl_cents_est")),
                      str(p.get("close_time") or "")[:16])
    console.print(table)


def _show_orders(toolbox: Toolbox) -> None:
    orders = toolbox.get_open_orders(NoInput())["orders"]
    table = Table("Order id", "Ticker", "Action", "Side", "Price", "Remaining")
    for o in orders:
        table.add_row(o["order_id"], o["ticker"], o["action"], o["side"],
                      f"{o['price_cents']}c", str(o["remaining_count"]))
    console.print(table if orders else "No resting orders.")


def _show_limits(toolbox: Toolbox) -> None:
    console.print_json(json.dumps(toolbox.get_risk_limits(NoInput())))


def _handle_command(cmd: str, toolbox: Toolbox) -> bool:
    """Handle a slash command. Returns False to quit."""
    parts = cmd.split()
    name = parts[0].lower()
    if name in ("/quit", "/exit"):
        return False
    if name == "/help":
        console.print(HELP)
    elif name == "/positions":
        _show_positions(toolbox)
    elif name == "/orders":
        _show_orders(toolbox)
    elif name == "/limits":
        _show_limits(toolbox)
    elif name == "/halt":
        toolbox.guard.halt()
        toolbox.audit.write("halt")
        console.print("[bold red]Trading halted.[/] New orders will be rejected.")
    elif name == "/resume":
        toolbox.guard.resume()
        toolbox.audit.write("resume")
        console.print("[bold green]Trading resumed.[/]")
    elif name == "/dryrun" and len(parts) == 2 and parts[1] in ("on", "off"):
        toolbox.dry_run = parts[1] == "on"
        toolbox.audit.write("dry_run", enabled=toolbox.dry_run)
        console.print(f"Dry run {'ON' if toolbox.dry_run else '[bold red]OFF[/]'}")
    else:
        console.print(f"Unknown command {cmd!r}. /help for commands.")
    return True


def build(settings: Settings) -> Toolbox:
    if not settings.kalshi_api_key_id:
        sys.exit("KALSHI_API_KEY_ID is not set (see .env.example).")
    key = load_private_key(settings.kalshi_private_key_path)
    client = KalshiClient(settings.base_url, settings.kalshi_api_key_id, key)
    limits = RiskLimits(
        max_order_cost_cents=settings.max_order_cost_cents,
        max_position_contracts_per_market=settings.max_position_contracts_per_market,
        max_total_exposure_cents=settings.max_total_exposure_cents,
        max_orders_per_session=settings.max_orders_per_session,
        max_session_loss_cents=settings.max_session_loss_cents,
        max_slippage_cents=settings.max_slippage_cents,
        allowed_ticker_prefixes=tuple(settings.allowed_ticker_prefixes),
        blocked_ticker_prefixes=tuple(settings.blocked_ticker_prefixes),
    )
    guard = RiskGuard(limits, kill_switch=settings.kill_switch)
    audit = AuditLog(settings.audit_log_path, settings.kalshi_env)
    return Toolbox(client, guard, audit, dry_run=settings.dry_run,
                   on_trade_event=_on_trade_event)


def main() -> None:
    settings = Settings()
    if settings.kalshi_env == "prod":
        console.print("[bold white on red] PRODUCTION [/] Orders will use real money.")
        if console.input("Type CONFIRM PROD to continue: ").strip() != "CONFIRM PROD":
            sys.exit("Aborted.")

    toolbox = build(settings)
    try:
        start_value = toolbox.start_session()
    except KalshiError as e:
        sys.exit(f"Could not reach Kalshi: {e}")

    env_tag = ("[bold white on red] PROD [/]" if settings.kalshi_env == "prod"
               else "[bold black on cyan] DEMO [/]")
    console.print(f"{env_tag} Kalshi agent · model {settings.kalshi_agent_model} · "
                  f"account value {_money(start_value)} · dry run "
                  f"{'ON' if toolbox.dry_run else '[bold red]OFF[/]'}"
                  f"{' · [bold red]HALTED[/]' if toolbox.guard.state.halted else ''}")
    console.print("Ask about or manage your positions. /help for commands.\n")

    def on_tool_call(name: str, args: Any) -> None:
        shown = json.dumps(args) if args else ""
        console.print(f"\n[dim]→ {name} {shown}[/dim]")

    agent = Agent(
        anthropic.Anthropic(), toolbox,
        model=settings.kalshi_agent_model, effort=settings.kalshi_agent_effort,
        on_text=lambda t: console.print(t, end="", markup=False, highlight=False),
        on_tool_call=on_tool_call,
        on_notice=lambda m: console.print(f"\n[yellow]{m}[/yellow]"),
    )

    while True:
        try:
            line = console.input("\n[bold]you ›[/bold] ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        if line.startswith("/"):
            try:
                if not _handle_command(line, toolbox):
                    break
            except KalshiError as e:
                console.print(f"[red]{e}[/red]")
            continue
        console.print("[bold]agent ›[/bold] ", end="")
        try:
            agent.send(line)
        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted.[/yellow]")
        except anthropic.APIError as e:
            console.print(f"\n[red]Claude API error: {e}[/red]")
        console.print()

    toolbox.client.close()
    toolbox.audit.write("session_end")
