"""Clean a dirty strategy config and write it to live/config/<SYMBOL>.json.

Usage:
    python live/clean_config.py BTCUSD_config.txt
    python live/clean_config.py PRO_SETUP.py --symbol GER40
    python live/clean_config.py dirty.txt --symbol '#BTCUSD' --force
"""

import argparse
import ast
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Set

import numpy as np

CONFIG_DIR = Path(__file__).resolve().parent / "config"

_ALLOWED_NAMES = {
    "np": np,
    "numpy": np,
    "nan": math.nan,
    "inf": math.inf,
    "True": True,
    "False": False,
    "None": None,
}

_ALLOWED_CALLS = {
    np.int8,
    np.int16,
    np.int32,
    np.int64,
    np.uint8,
    np.uint16,
    np.uint32,
    np.uint64,
    np.float16,
    np.float32,
    np.float64,
    np.bool_,
    np.array,
    np.asarray,
    float,
    int,
    str,
    bool,
}

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$", re.IGNORECASE)
_TIME_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*$")


def sanitize_symbol(symbol: str) -> str:
    sanitized = symbol.strip().upper().lstrip("#")
    for char in '<>:"/\\|?* ':
        sanitized = sanitized.replace(char, "_")
    return sanitized


def derive_symbol(input_path: Path, explicit: Optional[str]) -> str:
    if explicit:
        return explicit.strip().upper()

    stem = input_path.stem
    symbol = re.sub(r"(?i)[_-]?config$", "", stem).strip()
    if not symbol:
        raise ValueError(
            f"Cannot derive a symbol from '{input_path.name}'. Pass --symbol explicitly."
        )
    return symbol.upper()


def _eval_node(node: ast.AST) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Dict):
        return {
            _eval_node(key): _eval_node(value)
            for key, value in zip(node.keys, node.values)
        }
    if isinstance(node, ast.List):
        return [_eval_node(element) for element in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_eval_node(element) for element in node.elts)
    if isinstance(node, ast.Set):
        return {_eval_node(element) for element in node.elts}
    if isinstance(node, ast.UnaryOp):
        operand = _eval_node(node.operand)
        if isinstance(node.op, ast.USub):
            return -operand
        if isinstance(node.op, ast.UAdd):
            return +operand
        if isinstance(node.op, ast.Not):
            return not operand
    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        operations = {
            ast.Add: lambda: left + right,
            ast.Sub: lambda: left - right,
            ast.Mult: lambda: left * right,
            ast.Div: lambda: left / right,
            ast.FloorDiv: lambda: left // right,
            ast.Mod: lambda: left % right,
            ast.Pow: lambda: left ** right,
        }
        for op_type, operation in operations.items():
            if isinstance(node.op, op_type):
                return operation()
    if isinstance(node, ast.Name):
        if node.id in _ALLOWED_NAMES:
            return _ALLOWED_NAMES[node.id]
        raise ValueError(f"Unsupported name in config: {node.id}")
    if isinstance(node, ast.Attribute):
        base = _eval_node(node.value)
        if base is np:
            return getattr(np, node.attr)
        raise ValueError(f"Unsupported attribute in config: {node.attr}")
    if isinstance(node, ast.Call):
        func = _eval_node(node.func)
        if func not in _ALLOWED_CALLS:
            raise ValueError(f"Unsupported call in config: {ast.unparse(node.func)}")
        args = [_eval_node(arg) for arg in node.args]
        kwargs = {kw.arg: _eval_node(kw.value) for kw in node.keywords}
        return func(*args, **kwargs)

    raise ValueError(f"Unsupported expression in config: {ast.unparse(node)}")


def extract_config(text: str) -> Dict[str, Any]:
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    tree = ast.parse(text)
    named: Dict[str, Any] = {}
    fallback: Optional[Dict[str, Any]] = None
    expressions = []

    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                try:
                    value = _eval_node(node.value)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    named[target.id] = value
                    fallback = value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
            try:
                value = _eval_node(node.value)
            except ValueError:
                continue
            if isinstance(value, dict):
                named[node.target.id] = value
                fallback = value
        elif isinstance(node, ast.Expr):
            try:
                value = _eval_node(node.value)
            except ValueError:
                continue
            if isinstance(value, dict):
                expressions.append(value)

    for name in ("CONFIG", "PRO_SETUP", "dirty_config", "config"):
        if name in named:
            return named[name]

    if expressions:
        return expressions[-1]
    if fallback is not None:
        return fallback

    raise ValueError("No config dictionary found in input")


def to_builtin(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): to_builtin(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_builtin(item) for item in value]
    if isinstance(value, np.generic):
        return value.item()
    return value


def parse_lookback(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("lookback_seconds cannot be a boolean")

    if isinstance(value, (int, np.integer)):
        return int(value)

    if isinstance(value, (float, np.floating)):
        if not float(value).is_integer():
            raise ValueError(f"lookback_seconds must be a whole number of seconds: {value}")
        return int(value)

    match = _DURATION_RE.match(str(value))
    if not match:
        raise ValueError(
            f"Invalid lookback value '{value}'. Expected seconds like 3600, '3600S', '60min' or '1h'."
        )

    amount = float(match.group(1))
    unit = match.group(2).lower()
    multipliers = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
    seconds = amount * multipliers[unit]

    if not seconds.is_integer():
        raise ValueError(f"lookback_seconds must be a whole number of seconds: {value}")
    return int(seconds)


def normalize_time(value: Any) -> str:
    match = _TIME_RE.match(str(value))
    if not match:
        raise ValueError(f"Invalid session time '{value}'. Expected HH:MM.")
    hour, minute, _ = match.groups()
    hour, minute = int(hour), int(minute)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Invalid session time '{value}'.")
    return f"{hour:02d}:{minute:02d}"


def first_scalar(value: Any, field: str) -> Any:
    if isinstance(value, (list, tuple)):
        if not value:
            raise ValueError(f"'{field}' is empty")
        return value[0]
    return value


def clean_config(dirty: Dict[str, Any], default: Dict[str, Any]) -> Dict[str, Any]:
    config = to_builtin(dirty)

    entry = config.setdefault("entry_conditions", {})
    if "lookback_period" in entry:
        entry.setdefault("lookback_seconds", entry["lookback_period"])
        del entry["lookback_period"]
    if "15min_buffer" in entry:
        entry.setdefault("buffer_pips", entry["15min_buffer"])
        del entry["15min_buffer"]
    if "lookback_seconds" in entry:
        entry["lookback_seconds"] = parse_lookback(entry["lookback_seconds"])

    risk = config.setdefault("risk_management", {})
    if "initial_sl_pips" not in risk and "initial_sl" in risk:
        risk["initial_sl_pips"] = risk.pop("initial_sl")
    if "initial_sl_pips" in risk:
        risk["initial_sl_pips"] = first_scalar(risk["initial_sl_pips"], "initial_sl_pips")

    session_constraints = config.get("session_constraints")
    if session_constraints is not None:
        if "entry_start" not in session_constraints:
            raise ValueError("'session_constraints.entry_start' is required")
        if "mandatory_close" not in session_constraints:
            raise ValueError("'session_constraints.mandatory_close' is required")

        day_open = session_constraints.get("day_open")
        if day_open is None:
            day_open = session_constraints["entry_start"]
            print(
                "Warning: 'session_constraints.day_open' is missing; "
                f"using entry_start ({day_open}) instead.",
                file=sys.stderr,
            )

        config["session"] = {
            "pre-trading": 1,
            "day_open": normalize_time(day_open),
            "trading_start": normalize_time(session_constraints["entry_start"]),
            "trading_end": normalize_time(session_constraints["mandatory_close"]),
            "ghost_start": normalize_time(
                session_constraints.get("ghost_open", session_constraints["entry_start"])
            ),
            "ghost_end": normalize_time(
                session_constraints.get("ghost_close", session_constraints["mandatory_close"])
            ),
        }
        del config["session_constraints"]
    elif "session" in config:
        for key, value in config["session"].items():
            if key != "pre-trading":
                config["session"][key] = normalize_time(value)
    else:
        raise ValueError(
            "Dirty config has no 'session_constraints' (or existing 'session') section"
        )

    return _deep_merge(default, config)


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = to_builtin(base)
    for key, value in override.items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_default(config_dir: Path) -> Dict[str, Any]:
    default_path = config_dir / "default.json"
    if not default_path.exists():
        return {}
    with open(default_path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Clean a dirty strategy config and write it to live/config/<SYMBOL>.json"
    )
    parser.add_argument("input", help="path to the dirty config file")
    parser.add_argument("--symbol", help="symbol name (defaults to the input filename)")
    parser.add_argument("--output", help="output path (defaults to live/config/<SYMBOL>.json)")
    parser.add_argument("--force", action="store_true", help="overwrite an existing output file")
    args = parser.parse_args()

    input_path = Path(args.input)
    try:
        text = input_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"Error reading {input_path}: {exc}", file=sys.stderr)
        return 1

    try:
        symbol = derive_symbol(input_path, args.symbol)
        dirty = extract_config(text)
        defaults = load_default(CONFIG_DIR)
        config = clean_config(dirty, defaults)
    except (ValueError, SyntaxError, json.JSONDecodeError) as exc:
        print(f"Error cleaning config: {exc}", file=sys.stderr)
        return 1

    output_path = (
        Path(args.output)
        if args.output
        else CONFIG_DIR / f"{sanitize_symbol(symbol)}.json"
    )

    if output_path.exists() and not args.force:
        print(
            f"{output_path} already exists. Use --force to overwrite it.",
            file=sys.stderr,
        )
        return 1

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2, ensure_ascii=False, allow_nan=False)
        handle.write("\n")

    print(f"Cleaned {symbol} config written to {output_path}")
    print(f"Session: {config['session']}")
    print(
        f"Initial SL: {config['risk_management']['initial_sl_pips']} pips | "
        f"Lookback: {config['entry_conditions']['lookback_seconds']}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
