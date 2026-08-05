from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List

import yaml


@dataclass
class ColumnInfo:
    name: str
    data_type: str
    nullable: bool
    mode: str = ""
    description: str = ""
    comparable: bool = True


@dataclass
class ObjectInfo:
    catalog: str
    schema: str
    name: str
    object_type: str
    columns: List[ColumnInfo]
    partition_fields: List[str]


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value or "").strip("_") or "unnamed"


def load_yaml(path: Path) -> Dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Config root must be a mapping: {path}")
    return data


def save_yaml(data: Dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        if hasattr(value, "keys"):
            return {k: plain(value[k]) for k in value.keys()}
        return [plain(v) for v in value]
    if hasattr(value, "keys"):
        return {k: plain(value[k]) for k in value.keys()}
    return str(value)


def schema_plan(left: ObjectInfo, right: ObjectInfo, config: Dict[str, Any]) -> Dict[str, Any]:
    ls = {c.name.lower(): c for c in left.columns}
    rs = {c.name.lower(): c for c in right.columns}
    mode = str(config.get("comparison_mode", "keyed")).lower()
    join_key_mode = str(config.get("join_key_mode", "manual")).lower()
    column_cfg = config.get("columns", {}) or {}
    excluded_cfg = {str(x).lower() for x in column_cfg.get("exclude", [])}
    if join_key_mode == "manual":
        keys = [str(x) for x in config.get("join_keys", [])]
    elif join_key_mode == "all_dimensions":
        dimension_types = {"STRING", "DATE", "DATETIME", "TIME", "TIMESTAMP", "BOOL", "BOOLEAN"}
        keys = [
            ls[key].name
            for key in sorted(ls.keys() & rs.keys())
            if ls[key].data_type == rs[key].data_type
            and ls[key].mode == rs[key].mode
            and ls[key].data_type.upper() in dimension_types
            and ls[key].comparable
            and rs[key].comparable
            and key not in excluded_cfg
        ]
    else:
        keys = []
    selected_cfg = {str(x).lower() for x in column_cfg.get("include", [])}
    column_mode = str(column_cfg.get("mode", "all_common")).lower()

    missing_left = [asdict(rs[k]) for k in sorted(rs.keys() - ls.keys())]
    missing_right = [asdict(ls[k]) for k in sorted(ls.keys() - rs.keys())]
    type_mismatches, mode_mismatches, compatible = [], [], []
    for key in sorted(ls.keys() & rs.keys()):
        l, r = ls[key], rs[key]
        if l.data_type != r.data_type:
            type_mismatches.append({"name": l.name, "left_type": l.data_type, "right_type": r.data_type})
            continue
        if l.mode != r.mode:
            mode_mismatches.append({"name": l.name, "left_mode": l.mode, "right_mode": r.mode})
            continue
        if l.comparable and r.comparable:
            compatible.append(l)

    blockers = []
    if join_key_mode not in {"manual", "all_dimensions"}:
        blockers.append(
            f"Unsupported join_key_mode '{join_key_mode}'. Use 'manual' or 'all_dimensions'."
        )
    for key in keys:
        lk = ls.get(key.lower())
        rk = rs.get(key.lower())
        if not lk or not rk:
            blockers.append(f"Join key '{key}' is missing on one side.")
        elif lk.data_type != rk.data_type:
            blockers.append(f"Join key '{key}' has incompatible types: {lk.data_type} vs {rk.data_type}.")
        elif lk.mode != rk.mode:
            blockers.append(f"Join key '{key}' has incompatible modes: {lk.mode} vs {rk.mode}.")
    if mode == "keyed" and not keys:
        blockers.append("Keyed comparison requires at least one join key.")

    key_names = {k.lower() for k in keys}
    compared, excluded = [], []
    for col in compatible:
        low = col.name.lower()
        if low in key_names:
            excluded.append({"name": col.name, "reason": "join_key"})
        elif low in excluded_cfg:
            excluded.append({"name": col.name, "reason": "configured_exclusion"})
        elif column_mode == "selected" and low not in selected_cfg:
            excluded.append({"name": col.name, "reason": "not_selected"})
        else:
            compared.append(asdict(col))
    for item in type_mismatches:
        excluded.append({"name": item["name"], "reason": "data_type_mismatch"})
    for item in mode_mismatches:
        excluded.append({"name": item["name"], "reason": "mode_mismatch"})

    schema_status = "FAILED" if blockers else "WARNING" if missing_left or missing_right or type_mismatches or mode_mismatches else "PASSED"
    return {
        "schema_status": schema_status,
        "blockers": blockers,
        "missing_in_left": missing_left,
        "missing_in_right": missing_right,
        "data_type_mismatches": type_mismatches,
        "mode_mismatches": mode_mismatches,
        "compared_columns": compared,
        "excluded_columns": excluded,
        "join_keys": keys,
        "join_key_mode": join_key_mode,
    }


def save_json(data: Dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

