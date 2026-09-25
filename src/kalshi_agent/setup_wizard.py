"""Interactive first-time setup: `python -m kalshi_agent.setup_wizard`.

Writes .env, installs the Kalshi private key as ./kalshi_private_key.pem, and verifies
both the Kalshi and Anthropic credentials. Secrets are read with getpass (not echoed).
"""

from __future__ import annotations

import getpass
import os
import shutil
import sys
from pathlib import Path

ENV_PATH = Path(".env")
EXAMPLE_PATH = Path(".env.example")
KEY_PATH = Path("kalshi_private_key.pem")


def _ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{prompt}{suffix}: ").strip()
    return answer or (default or "")


def _find_pem_files() -> list[Path]:
    places = [Path.cwd(), Path.home() / "Downloads", Path.home() / "Desktop"]
    found: list[Path] = []
    for place in places:
        if place.is_dir():
            found += sorted(place.glob("*.pem"), key=lambda p: p.stat().st_mtime, reverse=True)
            found += sorted(place.glob("*.key"), key=lambda p: p.stat().st_mtime, reverse=True)
    return found


def _choose_key_file() -> Path:
    if KEY_PATH.exists() and _ask(f"Use existing {KEY_PATH}? (y/n)", "y").lower() == "y":
        return KEY_PATH
    candidates = [p for p in _find_pem_files() if p.resolve() != KEY_PATH.resolve()]
    if candidates:
        print("\nKey files found:")
        for i, p in enumerate(candidates, 1):
            print(f"  {i}. {p}")
        choice = _ask("Pick the Kalshi private key by number, or paste a path", "1")
        src = candidates[int(choice) - 1] if choice.isdigit() else Path(choice).expanduser()
    else:
        src = Path(_ask("Path to the Kalshi private key file (.pem) you downloaded")).expanduser()
    if not src.is_file():
        sys.exit(f"Not found: {src}")
    shutil.copyfile(src, KEY_PATH)
    os.chmod(KEY_PATH, 0o600)
    print(f"Copied {src.name} -> {KEY_PATH} (permissions 600)")
    return KEY_PATH


def _write_env(values: dict[str, str]) -> None:
    base = ENV_PATH if ENV_PATH.exists() else EXAMPLE_PATH
    lines = base.read_text().splitlines() if base.exists() else []
    seen: set[str] = set()
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if not line.lstrip().startswith("#") and key in values:
            out.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in values.items() if k not in seen]
    ENV_PATH.write_text("\n".join(out) + "\n")
    os.chmod(ENV_PATH, 0o600)


def main() -> None:
    print("Kalshi agent setup. Keys you type are hidden and only saved to .env on this Mac.\n")
    env = _ask("Kalshi environment: demo or prod", "demo").lower()
    if env not in ("demo", "prod"):
        sys.exit("Environment must be 'demo' or 'prod'.")
    site = "demo.kalshi.co" if env == "demo" else "kalshi.com"
    print(f"Create an API key on {site} under Account -> API Keys if you haven't yet.")
    key_id = getpass.getpass("Kalshi API Key ID (hidden): ").strip()
    if not key_id:
        sys.exit("Kalshi API Key ID is required.")
    key_file = _choose_key_file()
    anthropic_key = getpass.getpass(
        "Anthropic API key from console.anthropic.com (hidden, Enter to skip): ").strip()

    values = {"KALSHI_ENV": env, "KALSHI_API_KEY_ID": key_id,
              "KALSHI_PRIVATE_KEY_PATH": f"./{key_file.name}", "DRY_RUN": "true"}
    if anthropic_key:
        values["ANTHROPIC_API_KEY"] = anthropic_key
    _write_env(values)
    print(f"\nSaved {ENV_PATH} (dry run ON).\n\nTesting Kalshi connection...")

    from .config import Settings
    from .kalshi_client import KalshiClient, KalshiError, cents, load_private_key

    s = Settings()
    try:
        client = KalshiClient(s.base_url, s.kalshi_api_key_id,
                              load_private_key(s.kalshi_private_key_path))
        balance = client.get_balance()
        positions = client.get_positions()
    except (KalshiError, ValueError) as e:
        print(f"  Kalshi FAILED: {e}")
        if "401" in str(e):
            print(f"  The Key ID and key file don't match, or the key wasn't created on {site}.")
        sys.exit(1)
    print(f"  Kalshi OK ({env}): cash ${(cents(balance, 'balance') or 0) / 100:,.2f}, "
          f"{len(positions)} open positions")

    if anthropic_key or os.environ.get("ANTHROPIC_API_KEY"):
        print("Testing Anthropic key...")
        import anthropic
        try:
            anthropic.Anthropic(api_key=anthropic_key or None).models.retrieve(
                s.kalshi_agent_model)
            print("  Anthropic OK")
        except anthropic.AuthenticationError:
            sys.exit("  Anthropic FAILED: key rejected. Re-run setup with a valid key.")
        except anthropic.APIError as e:
            print(f"  Anthropic check inconclusive: {e}")
    else:
        print("No Anthropic key yet; add ANTHROPIC_API_KEY to .env before starting the agent.")

    print("\nAll set. Start the agent with:  python -m kalshi_agent")


if __name__ == "__main__":
    main()
