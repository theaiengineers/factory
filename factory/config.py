"""Reads a building's `factory.yml` (spec/factory-file.md) and checks the parts the engine uses.

Only the parts that code in this package reads are checked. Keys the engine does not use yet are kept as they are.
"""
from __future__ import annotations

import yaml

FILE = "factory.yml"
SPEC_PREFIX = "factory/0."
REVIEW = "independent-review"
AUTOMATIC_LEVELS = ("code", "high")      # the levels an automatic reviewer's pass can be allowed to count for


class ConfigError(ValueError):
    """The factory file is missing or cannot be used. The message is one plain sentence."""


def _strings(value, where: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ConfigError(f"{where} must be a list of paths")
    return value


def _automatic(value, where: str) -> dict | None:
    """The review station's `automatic` reviewer: {"account", "passes"}, or None when there is none.

    The account must be a bot's login (it ends `[bot]`). The gate takes a record from this account at its word, with
    no permission lookup, so naming a person here would let that person's records count whatever they may do."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ConfigError(f"{where}.automatic must be a mapping with `account`")
    account = value.get("account")
    if not isinstance(account, str) or not account.endswith("[bot]") or len(account) <= len("[bot]"):
        raise ConfigError(f"{where}.automatic.account must be a bot's login, such as github-actions[bot]")
    passes = value.get("passes", ["code"])
    if not isinstance(passes, list) or not passes or not all(p in AUTOMATIC_LEVELS for p in passes):
        raise ConfigError(f"{where}.automatic.passes must list code, or code and high")
    return {"account": account, "passes": list(dict.fromkeys(passes))}


def parse(text: str) -> dict:
    """The factory file as {"review": the independent-review station or None, "risk": [...], "raw": everything}."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"{FILE} is not valid YAML: {str(e).splitlines()[0]}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{FILE} must be a mapping")
    if not str(raw.get("spec", "")).startswith(SPEC_PREFIX):
        raise ConfigError(f"{FILE} must say `spec: factory/0.1`")

    review = None
    intake = raw["intake"] if raw.get("intake") is not None else []
    if not isinstance(intake, list):
        raise ConfigError("intake must be a list of stations")
    for station in intake:
        if not isinstance(station, dict) or not station.get("station") or not station.get("uses"):
            raise ConfigError("every intake station needs `station` and `uses`")
        if station["uses"] == REVIEW:
            if review is not None:
                raise ConfigError(f"intake has more than one {REVIEW} station")
            review = {"station": station["station"], "standard": station.get("standard"),
                      "skip_when_only": _strings(station.get("skip_when_only"), f"{station['station']}.skip_when_only"),
                      "automatic": _automatic(station.get("automatic"), station["station"])}

    risk = []
    areas = raw["risk"] if raw.get("risk") is not None else []
    if not isinstance(areas, list):
        raise ConfigError("risk must be a list of areas")
    for area in areas:
        if not isinstance(area, dict) or not area.get("area"):
            raise ConfigError("every risk entry needs `area`")
        if not area.get("why"):
            raise ConfigError(f"risk area {area['area']!r} needs `why`: it is shown to the reviewer")
        paths = _strings(area.get("paths"), f"risk area {area['area']!r} paths")
        if not paths:
            raise ConfigError(f"risk area {area['area']!r} has no paths")
        risk.append({"area": str(area["area"]), "why": str(area["why"]), "paths": paths})

    return {"review": review, "risk": risk, "raw": raw}
