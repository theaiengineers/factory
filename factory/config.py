"""Reads a building's `factory.yml` (spec/factory-file.md) and checks the parts the engine uses.

Only the parts that code in this package reads are checked. Keys the engine does not use yet are kept as they are.
"""
from __future__ import annotations

import datetime
import re

import yaml

FILE = "factory.yml"
SPEC_PREFIX = "factory/0."
REVIEW = "independent-review"
BOT_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\[bot\]")
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
    """The review station's `automatic` reviewer: {"account", "passes"} and, when the file gives one,
    "credential_renewed" (a day, YYYY-MM-DD); or None when there is none.

    `credential_renewed` is only read by the reviewer health check (reviewer_health.py); the gate ignores it.
    The account must be a bot's login (it ends `[bot]`). The gate takes a record from this account at its word, with
    no permission lookup, so naming a person here would let that person's records count whatever they may do."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ConfigError(f"{where}.automatic must be a mapping with `account`")
    unknown = sorted((k for k in value if k not in ("account", "passes", "credential_renewed")), key=str)
    if unknown:
        raise ConfigError(f"{where}.automatic has a key it does not know: {unknown[0]}")
    account = value.get("account")
    if not isinstance(account, str) or not BOT_LOGIN.fullmatch(account):
        raise ConfigError(f"{where}.automatic.account must be a bot's login, such as github-actions[bot]")
    passes = value.get("passes", ["code"])
    if not isinstance(passes, list) or "code" not in passes or not all(p in AUTOMATIC_LEVELS for p in passes):
        raise ConfigError(f"{where}.automatic.passes must list code, or code and high")
    out = {"account": account, "passes": list(dict.fromkeys(passes))}
    renewed = _day(value.get("credential_renewed"), f"{where}.automatic.credential_renewed")
    return {**out, "credential_renewed": renewed} if renewed else out


def _day(value, where: str) -> str | None:
    """A calendar day as YYYY-MM-DD, or None. YAML reads an unquoted date as a date; a quoted one arrives as text."""
    if value is None:
        return None
    if isinstance(value, datetime.date) and not isinstance(value, datetime.datetime):
        return value.isoformat()
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            return datetime.date.fromisoformat(value).isoformat()
        except ValueError:
            pass
    raise ConfigError(f"{where} must be a day written YYYY-MM-DD")


def parse(text: str) -> dict:
    """The factory file as {"review": the independent-review station or None, "risk": [...], "raw": everything}."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"{FILE} is not valid YAML: {str(e).splitlines()[0]}") from e
    except ValueError as e:
        # PyYAML lets a plain ValueError out for a value it cannot build: an unquoted value shaped like a date that
        # is not a real day, or a number too long to convert.
        raise ConfigError(f"{FILE} is not valid YAML: it has a value that cannot be read ({str(e).splitlines()[0][:120]})") from e
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
