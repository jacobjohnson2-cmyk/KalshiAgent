"""Interactive first-time setup: `python -m kalshi_agent.setup_wizard`.

Writes .env, installs the Kalshi private key as ./kalshi_private_key.pem, and verifies
both the Kalshi and Anthropic credentials. Secrets are read with getpass (not echoed).
"""

from __future__ import annotations

import getpass
import os
import shutil
import subprocess
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


def _valid_rsa_pem(data: bytes) -> bool:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    try:
        key = serialization.load_pem_private_key(data, password=None)
    except (ValueError, TypeError):
        return False
    return isinstance(key, rsa.RSAPrivateKey)


def _save_key(data: bytes, source: str) -> Path:
    if not _valid_rsa_pem(data):
        sys.exit(
            f"That {source} is not a complete RSA private key. Copy the whole key, including "
            "the '-----BEGIN ... PRIVATE KEY-----' and '-----END ... PRIVATE KEY-----' lines, "
            "and run setup again."
        )
    KEY_PATH.write_bytes(data.strip() + b"\n")
    os.chmod(KEY_PATH, 0o600)
    print(f"Saved private key from {source} -> {KEY_PATH} (permissions 600)")
    return KEY_PATH


def _key_from_clipboard() -> Path:
    if not shutil.which("pbpaste"):
        sys.exit("Clipboard reading is only supported on macOS. Save the key to a file instead.")
    input("Copy the ENTIRE private key on the Kalshi page (including the BEGIN and END "
          "lines), then press Enter here...")
    data = subprocess.run(["pbpaste"], capture_output=True, check=True).stdout
    path = _save_key(data, "clipboard")
    subprocess.run(["pbcopy"], input=b"", check=False)  # don't leave the key on the clipboard
    print("Clipboard cleared.")
    return path


def _choose_key_file() -> Path:
    if KEY_PATH.exists() and _valid_rsa_pem(KEY_PATH.read_bytes()) and \
            _ask(f"Use existing {KEY_PATH}? (y/n)", "y").lower() == "y":
        return KEY_PATH
    candidates = [p for p in _find_pem_files() if p.resolve() != KEY_PATH.resolve()]
    print("\nWhere is your Kalshi private key?")
    for i, p in enumerate(candidates, 1):
        print(f"  {i}. {p}")
    print("  c. It's shown as text on the Kalshi page - read it from the clipboard")
    print("  Or type the path to the .pem file.")
    choice = _ask("Choice", "1" if candidates else "c")
    if choice.lower() == "c":
        return _key_from_clipboard()
    if choice.isdigit() and 1 <= int(choice) <= len(candidates):
        src = candidates[int(choice) - 1]
    else:
        src = Path(choice).expanduser()
    if not src.is_file():
        sys.exit(f"Not found: {src}")
    return _save_key(src.read_bytes(), src.name)


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
    env = ""
    while env not in ("demo", "prod"):
        env = _ask("Kalshi environment - type demo or prod (just press Enter for demo)",
                   "demo").lower()
        if env not in ("demo", "prod"):
            print("  Please type exactly: demo or prod. (Don't paste keys here.)")
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
