from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SECRETS_DIR = Path(os.environ.get("BITINVEST_SECRETS_DIR", REPO_ROOT / "secrets"))
DATA_DIR = Path(os.environ.get("BITINVEST_DATA_DIR", REPO_ROOT / "data"))
STEP_CSV = DATA_DIR / "step.csv"


@dataclass
class TokenGroup:
    tokens: list[str]
    weights: list[float]


def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open("r") as f:
        return [line.strip() for line in f if line.strip()]


def load_token_group(token_file: str, weight_file: str) -> TokenGroup:
    tokens = _read_lines(SECRETS_DIR / token_file)
    weights = [float(w) for w in _read_lines(SECRETS_DIR / weight_file)]
    if not tokens:
        raise ValueError(f"{token_file} is empty — need at least one token")
    if len(tokens) != len(weights):
        raise ValueError(
            f"{token_file} has {len(tokens)} token(s) but {weight_file} has "
            f"{len(weights)} weight(s) — they must match 1:1"
        )
    return TokenGroup(tokens=tokens, weights=weights)


def load_master_config() -> TokenGroup:
    return load_token_group("master_tokens.txt", "master_weights.txt")


def load_slave_config() -> TokenGroup:
    return load_token_group("slave_tokens.txt", "slave_weights.txt")
