"""Model access: environment keys, list prices, the spend ledger with its caps, and the one function that calls the Claude API."""

import functools
import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import anthropic

# USD per million tokens at list price (2026-10-17).
# A cache write bills at 1.25x input. A cache read bills at 0.05x input on Sonnet and 0.1x on Haiku.
PRICES: dict[str, dict[str, float]] = {
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_write": 2.50, "cache_read": 0.10},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_write": 0.125, "cache_read": 0.01},
}
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
_PRICE_KEYS = dict(zip(USAGE_KEYS, ("input", "output", "cache_write", "cache_read")))


def load_env_file(path: Path) -> None:
    """Set each missing environment variable from the KEY=value lines of a file. A missing file changes nothing."""
    if not Path(path).is_file():
        return
    for line in Path(path).read_text().splitlines():
        line = line.strip().removeprefix("export ").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def usage_cost(model: str, usage: dict) -> float:
    price = PRICES[model]  # a model without a price raises KeyError: it must never be billed at zero
    return sum(price[_PRICE_KEYS[key]] * (usage.get(key) or 0) for key in USAGE_KEYS) / 1e6


class BudgetExceeded(RuntimeError):
    """A charge would pass the cap of its account. The batch stops."""


class Budget:
    """Spend per account ("claude" or "jev"), saved to a JSON ledger after every charge, with a cap per account."""

    def __init__(self, ledger: Path, caps: dict[str, float]):
        self.ledger, self.caps = Path(ledger), caps
        # ponytail: the lock covers the threads of one process.
        # Two commands writing one ledger can lose a charge; add a file lock then.
        self._lock = threading.Lock()

    def _load(self) -> dict:
        saved = json.loads(self.ledger.read_text()) if self.ledger.exists() else {}
        return {account: {"total_usd": 0.0, "entries": []} for account in self.caps} | saved

    def spent(self, account: str) -> float:
        return self._load()[account]["total_usd"]

    def charge(self, account: Literal["claude", "jev"], cost: float, note: str) -> None:
        """Add the cost of one call. Over the cap, raise BudgetExceeded and record nothing."""
        with self._lock:
            ledger = self._load()
            total = ledger[account]["total_usd"] + cost
            if total > self.caps[account] + 1e-9:  # the tolerance absorbs float sums such as 0.1 + 0.2
                raise BudgetExceeded(f"{account} spend would reach ${total:.4f}, over the ${self.caps[account]:.2f} cap ({note})")
            ledger[account]["total_usd"] = total
            ledger[account]["entries"].append({"at": datetime.now(UTC).isoformat(timespec="seconds"), "usd": cost, "note": note})
            self.ledger.parent.mkdir(parents=True, exist_ok=True)
            scratch = self.ledger.with_name(self.ledger.name + ".tmp")
            scratch.write_text(json.dumps(ledger, indent=2))
            scratch.replace(self.ledger)  # a crash never leaves half a ledger


@functools.cache
def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(max_retries=4)  # reads ANTHROPIC_API_KEY; the SDK retries 429 and 5xx with backoff


def call_anthropic(model: str, system: str, messages: list[dict], tools: list[dict], max_tokens: int = 3000) -> dict:
    """One Messages API call with the system prompt cached. Returns plain data: content blocks, usage counts, stop reason.

    This is the only function that touches the SDK. Tests replace it with a fake.
    """
    options = {"tools": tools, "tool_choice": {"type": "auto"}} if tools else {}  # the API rejects tool_choice without tools
    response = _client().messages.create(
        model=model,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=messages,
        **options,
    )
    return {
        "content": [block.model_dump(exclude_none=True) for block in response.content],
        "usage": {key: getattr(response.usage, key) or 0 for key in USAGE_KEYS},
        "stop_reason": response.stop_reason,
    }
