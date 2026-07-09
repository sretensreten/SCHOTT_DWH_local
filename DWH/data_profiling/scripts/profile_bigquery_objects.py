#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None

try:
    from google.cloud import bigquery
except Exception as exc:
    print("[ERROR] google-cloud-bigquery is not installed or not importable.")
    print("        Install: pip install google-cloud-bigquery python-dotenv")
    print(f"Details: {exc}")
    sys.exit(1)

DEFAULT_PROJECT_CONFIG_PATH = Path("DWH") / "data_profiling" / "config" / "profiler_project.json"
ALL_DATASETS_LABEL = "All datasets"
PROFILE_VERSION = "bigquery-one-pass-20260709_1531"

NUMERIC_TYPES = {"INT64", "INTEGER", "FLOAT", "FLOAT64", "NUMERIC", "BIGNUMERIC"}
TEMPORAL_TYPES = {"DATE", "DATETIME", "TIMESTAMP", "TIME"}
STRING_TYPES = {"STRING"}
BOOL_TYPES = {"BOOL", "BOOLEAN"}
COMPLEX_TYPES = {"ARRAY", "STRUCT", "RECORD", "GEOGRAPHY", "JSON"}


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def get_cfg(config: Dict[str, Any], path: str, default: Any = None) -> Any:
    cur: Any = config
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_profiler_config_path(project_config_path: Path) -> Path:
    if not project_config_path.is_file():
        raise FileNotFoundError(f"Profiler project config not found: {project_config_path}")
    project_config = load_json(project_config_path)
    config_value = project_config.get("active_profiler_config")
    if not config_value:
        raise ValueError(f"Missing active_profiler_config in {project_config_path}")
    config_path = Path(config_value)
    if config_path.is_absolute() or config_path.is_file():
        return config_path
    return project_config_path.parent / config_path


def load_env(config: Dict[str, Any]) -> None:
    if load_dotenv is None:
        return
    candidates: List[Path] = []
    env_file = get_cfg(config, "connection.env_file", ".env")
    if env_file:
        candidates.append(Path(env_file))
    candidates.extend([Path.cwd() / ".env", Path.cwd() / ".test" / ".env"])
    seen = set()
    for p in candidates:
        key = str(p.resolve()) if p.exists() else str(p)
        if key in seen:
            continue
        seen.add(key)
        if p.is_file():
            load_dotenv(p)
            log(f"Loaded env file: {p}")
            return


def resolve_project_id(config: Dict[str, Any]) -> str:
    value = (
        get_cfg(config, "bigquery.project_id")
        or os.getenv(get_cfg(config, "connection.project_id_env", "GCP_PROJECT_ID"))
        or os.getenv("GOOGLE_CLOUD_PROJECT")
    )
    if not value:
        raise ValueError("Missing BigQuery project id. Set bigquery.project_id, GCP_PROJECT_ID, or GOOGLE_CLOUD_PROJECT.")
    return str(value).strip()


def create_client(config: Dict[str, Any]):
    load_env(config)
    project_id = resolve_project_id(config)
    location = get_cfg(config, "bigquery.location") or os.getenv("BQ_LOCATION")
    log("Credentials: Application Default Credentials / Google Cloud SDK")
    log(f"BigQuery project: {project_id}")
    if location:
        log(f"BigQuery location: {location}")
    return bigquery.Client(project=project_id, location=location or None), project_id


def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name or "").strip("_") or "unnamed"


def quote_ident(part: str) -> str:
    return "`" + str(part).replace("`", "``") + "`"


def table_ref(project_id: str, dataset_id: str, table_name: str) -> str:
    return f"{quote_ident(project_id)}.{quote_ident(dataset_id)}.{quote_ident(table_name)}"


def field_ref(field_name: str) -> str:
    return quote_ident(field_name)


def classify_type(t: str) -> str:
    v = (t or "").upper().strip()
    if v in NUMERIC_TYPES:
        return "numeric"
    if v in TEMPORAL_TYPES:
        return "temporal"
    if v in STRING_TYPES or v in BOOL_TYPES:
        return "categorical"
    return "other"


def normalize_bq_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def row_to_plain(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return {k: row_to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        if hasattr(value, "keys"):
            return {k: row_to_plain(value[k]) for k in value.keys()}
        return [row_to_plain(v) for v in value]
    if hasattr(value, "keys"):
        return {k: row_to_plain(value[k]) for k in value.keys()}
    return normalize_bq_value(value)


def object_type(table) -> str:
    t = (getattr(table, "table_type", "") or "").upper()
    return "VIEW" if t in {"VIEW", "MATERIALIZED_VIEW"} else "TABLE"


def list_datasets(client, project_id: str) -> List[str]:
    return sorted([d.dataset_id for d in client.list_datasets(project=project_id)])


def list_objects(client, project_id: str, dataset_id: str) -> List[Any]:
    return sorted(list(client.list_tables(f"{project_id}.{dataset_id}")), key=lambda x: x.table_id.lower())


def configured_datasets(config: Dict[str, Any]) -> List[str]:
    raw = get_cfg(config, "bigquery.datasets", []) or []
    if not isinstance(raw, list):
        return []
    return [str(x).strip() for x in raw if str(x).strip()]


def default_dataset_scope(client, config: Dict[str, Any], project_id: str) -> List[str]:
    configured = configured_datasets(config)
    if configured:
        return list_datasets(client, project_id) if configured == ["*"] else configured
    env_dataset = os.getenv(get_cfg(config, "connection.dataset_id_env", "BQ_DATASET_ID"))
    return [env_dataset.strip()] if env_dataset else list_datasets(client, project_id)


def parse_menu_selection(value: str, count: int, allow_all: bool = True) -> Optional[List[int]]:
    raw = (value or "").strip().lower()
    if raw in {"q", "quit", "exit"}:
        return None
    if allow_all and raw in {"all", "a", "*"}:
        return list(range(count))
    selected, seen = [], set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            left, right = [x.strip() for x in part.split("-", 1)]
            if not left.isdigit() or not right.isdigit():
                raise ValueError(f"Invalid range: {part}")
            start, end = int(left), int(right)
            if start > end:
                raise ValueError(f"Invalid range: {part}")
            numbers = range(start, end + 1)
        else:
            if not part.isdigit():
                raise ValueError(f"Invalid value: {part}")
            numbers = [int(part)]
        for number in numbers:
            if number < 1 or number > count:
                raise ValueError(f"Selection out of range: {number}")
            idx = number - 1
            if idx not in seen:
                selected.append(idx)
                seen.add(idx)
    if not selected:
        raise ValueError("No selection entered.")
    return selected


def choose_datasets(client, config: Dict[str, Any], project_id: str) -> Optional[List[str]]:
    values = default_dataset_scope(client, config, project_id)
    values = list(values) + [ALL_DATASETS_LABEL]
    print("\nChoose BigQuery dataset")
    print("=======================")
    for i, v in enumerate(values, 1):
        print(f"[{i}] {v}")
    print("\nEnter dataset numbers separated by comma, e.g. 1,2,3 or 2,3")
    print("Use ranges like 1-3, enter 'all' to select configured menu items, or q to exit.")
    print(f"If you include '{ALL_DATASETS_LABEL}', a second menu with all project datasets will be shown.")
    while True:
        raw = input("Selection: ").strip()
        try:
            indexes = parse_menu_selection(raw, len(values), allow_all=True)
        except ValueError as exc:
            print(f"Invalid selection: {exc}")
            continue
        if indexes is None:
            return None
        chosen = [values[i] for i in indexes]
        if ALL_DATASETS_LABEL not in chosen:
            return chosen
        all_datasets = list_datasets(client, project_id)
        print("\nChoose from all project datasets")
        print("================================")
        for i, ds in enumerate(all_datasets, 1):
            print(f"[{i}] {ds}")
        print("\nEnter dataset numbers separated by comma, e.g. 1,2,3 or 4,5")
        print("Use ranges like 1-5, enter 'all' to select all project datasets, or q to exit.")
        while True:
            raw_all = input("Dataset selection: ").strip()
            try:
                all_indexes = parse_menu_selection(raw_all, len(all_datasets), allow_all=True)
            except ValueError as exc:
                print(f"Invalid selection: {exc}")
                continue
            if all_indexes is None:
                return None
            return [all_datasets[i] for i in all_indexes]


def include_object(config: Dict[str, Any], table, contains: str, starts: str) -> bool:
    typ = object_type(table)
    if typ == "TABLE" and not bool(get_cfg(config, "profiling.include_tables", True)):
        return False
    if typ == "VIEW" and not bool(get_cfg(config, "profiling.include_views", False)):
        return False
    name = table.table_id
    if contains and contains.lower() not in name.lower():
        return False
    if starts and not name.lower().startswith(starts.lower()):
        return False
    return True


def selected_objects(client, config: Dict[str, Any], project_id: str, datasets: Optional[List[str]], contains: str = "", starts: str = "") -> List[Dict[str, Any]]:
    dataset_scope = datasets or default_dataset_scope(client, config, project_id)
    out = []
    total = len(dataset_scope)
    if total > 1:
        print(f"\nRetrieving objects from {total} dataset(s)...")
    for index, ds in enumerate(dataset_scope, 1):
        if total > 1:
            print(f"[{index}/{total}] Listing objects in dataset: {ds}", flush=True)
        try:
            items = list_objects(client, project_id, ds)
        except KeyboardInterrupt:
            print("\n[STOPPED] Object retrieval interrupted by user.")
            raise
        except Exception as exc:
            print(f"[WARN] Cannot list objects for dataset {ds}: {exc}")
            continue
        for item in items:
            if include_object(config, item, contains, starts):
                out.append({"dataset": ds, "table_id": item.table_id, "list_item": item})
    if total > 1:
        print(f"Retrieved {len(out)} matching object(s).")
    return out


def choose_objects_interactive(objs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not objs:
        return []
    print("\nChoose BigQuery objects")
    print("=======================")
    for i, o in enumerate(objs, 1):
        print(f"[{i}] {o['dataset']}.{o['table_id']} | {object_type(o['list_item'])}")
    print("\nEnter object numbers separated by comma, e.g. 1,2,3 or 4,5")
    print("Use ranges like 1-5, enter 'all' to select all listed objects, or q to exit.")
    while True:
        raw = input("Selection: ").strip()
        try:
            indexes = parse_menu_selection(raw, len(objs), allow_all=True)
        except ValueError as exc:
            print(f"Invalid selection: {exc}")
            continue
        if indexes is None:
            return []
        return [objs[i] for i in indexes]


def columns_of(table) -> List[Dict[str, Any]]:
    return [
        {
            "name": f.name,
            "type": f.field_type,
            "mode": f.mode,
            "comment": f.description or "",
            "description": f.description or "",
        }
        for f in table.schema
    ]


def schema_type_lookup(table) -> Dict[str, str]:
    return {f.name.lower(): f.field_type.upper() for f in table.schema}


def partition_filter_for_table(config: Dict[str, Any], table, is_view: bool) -> Tuple[str, Dict[str, Any]]:
    if not bool(get_cfg(config, "profiling.use_partition_filter", True)):
        return "", {"enabled": False, "reason": "disabled"}
    window_days = int(get_cfg(config, "profiling.partition_window_days", 90))
    date_expr = f"DATE_SUB(CURRENT_DATE(), INTERVAL {window_days} DAY)"
    type_by_name = schema_type_lookup(table)

    if not is_view and getattr(table, "time_partitioning", None):
        field = getattr(table.time_partitioning, "field", None)
        if field:
            ftype = type_by_name.get(field.lower(), "")
            col = field_ref(field)
            if ftype == "DATE":
                return f"{col} >= {date_expr}", {"enabled": True, "window_days": window_days, "type": "partition_column", "column": field}
            if ftype == "DATETIME":
                return f"{col} >= DATETIME({date_expr})", {"enabled": True, "window_days": window_days, "type": "partition_column", "column": field}
            if ftype == "TIMESTAMP":
                return f"{col} >= TIMESTAMP({date_expr})", {"enabled": True, "window_days": window_days, "type": "partition_column", "column": field}
            return "", {"enabled": False, "reason": f"unsupported_partition_type:{ftype}", "column": field}
        return f"_PARTITIONDATE >= {date_expr}", {"enabled": True, "window_days": window_days, "type": "ingestion_time", "column": "_PARTITIONDATE"}

    if is_view and bool(get_cfg(config, "profiling.apply_window_to_views", False)):
        candidates = get_cfg(config, "profiling.view_date_filter_columns", []) or []
        for field in candidates:
            ftype = type_by_name.get(str(field).lower(), "")
            if not ftype:
                continue
            col = field_ref(str(field))
            if ftype == "DATE":
                return f"{col} >= {date_expr}", {"enabled": True, "window_days": window_days, "type": "view_column", "column": str(field)}
            if ftype == "DATETIME":
                return f"{col} >= DATETIME({date_expr})", {"enabled": True, "window_days": window_days, "type": "view_column", "column": str(field)}
            if ftype == "TIMESTAMP":
                return f"{col} >= TIMESTAMP({date_expr})", {"enabled": True, "window_days": window_days, "type": "view_column", "column": str(field)}
        return "", {"enabled": False, "reason": "view_window_column_not_found"}

    if is_view:
        return "", {"enabled": False, "reason": "views_not_windowed"}
    return "", {"enabled": False, "reason": "not_partitioned"}


def source_sql(project_id: str, dataset_id: str, table_name: str, limit: int, where_filter: str = "") -> str:
    base = table_ref(project_id, dataset_id, table_name)
    where_clause = f" WHERE {where_filter}" if where_filter else ""
    limit_clause = f" LIMIT {int(limit)}" if limit and limit > 0 else ""
    return f"(SELECT * FROM {base}{where_clause}{limit_clause})" if where_clause or limit_clause else base


def column_struct_sql(col: Dict[str, Any], alias: str, top_n: int) -> str:
    cname = col["name"]
    ctype = (col.get("type") or "").upper().strip()
    mode = (col.get("mode") or "").upper().strip()
    q = field_ref(cname)
    kind = classify_type(ctype)

    if mode == "REPEATED" or ctype in {"ARRAY", "STRUCT", "RECORD"}:
        return (
            "STRUCT(\n"
            "    CAST(NULL AS INT64) AS null_count,\n"
            f"    'Unsupported repeated or complex field for one-pass scalar profiling: {ctype or mode}' AS profile_error\n"
            f"  ) AS {quote_ident(alias)}"
        )

    if kind == "numeric":
        return (
            "STRUCT(\n"
            f"    COUNTIF({q} IS NULL) AS null_count,\n"
            f"    MIN({q}) AS min_value,\n"
            f"    MAX({q}) AS max_value\n"
            f"  ) AS {quote_ident(alias)}"
        )

    if kind == "temporal":
        return (
            "STRUCT(\n"
            f"    COUNTIF({q} IS NULL) AS null_count,\n"
            f"    CAST(MIN({q}) AS STRING) AS min_value,\n"
            f"    CAST(MAX({q}) AS STRING) AS max_value\n"
            f"  ) AS {quote_ident(alias)}"
        )

    if ctype in STRING_TYPES:
        return (
            "STRUCT(\n"
            f"    COUNTIF({q} IS NULL) AS null_count,\n"
            f"    APPROX_COUNT_DISTINCT({q}) AS distinct_count,\n"
            f"    MIN(LENGTH(CAST({q} AS STRING))) AS min_length,\n"
            f"    MAX(LENGTH(CAST({q} AS STRING))) AS max_length,\n"
            f"    APPROX_TOP_COUNT(CAST({q} AS STRING), {int(top_n)}) AS top_values\n"
            f"  ) AS {quote_ident(alias)}"
        )

    if ctype in BOOL_TYPES:
        return (
            "STRUCT(\n"
            f"    COUNTIF({q} IS NULL) AS null_count,\n"
            f"    APPROX_COUNT_DISTINCT({q}) AS distinct_count,\n"
            f"    MIN(CAST({q} AS STRING)) AS min_value,\n"
            f"    MAX(CAST({q} AS STRING)) AS max_value,\n"
            f"    APPROX_TOP_COUNT(CAST({q} AS STRING), {int(top_n)}) AS top_values\n"
            f"  ) AS {quote_ident(alias)}"
        )

    # Best-effort for unsupported scalar types (for example GEOGRAPHY / JSON): null_count only.
    return (
        "STRUCT(\n"
        f"    COUNTIF({q} IS NULL) AS null_count\n"
        f"  ) AS {quote_ident(alias)}"
    )


def profile_query_sql(project_id: str, dataset_id: str, table_name: str, cols: List[Dict[str, Any]], top_n: int, limit: int, where_filter: str) -> str:
    src = source_sql(project_id, dataset_id, table_name, limit, where_filter)
    select_parts = ["COUNT(1) AS __profiled_rows"]
    for idx, col in enumerate(cols):
        select_parts.append(column_struct_sql(col, f"__col_{idx}", top_n))
    return "SELECT\n  " + ",\n  ".join(select_parts) + f"\nFROM {src}"


def make_query_job_config(config: Dict[str, Any], *, dry_run: bool = False):
    kwargs: Dict[str, Any] = {"dry_run": dry_run, "use_query_cache": False}
    max_bytes = int(get_cfg(config, "cost_control.max_bytes_billed", 0) or 0)
    if max_bytes > 0 and not dry_run:
        kwargs["maximum_bytes_billed"] = max_bytes
    return bigquery.QueryJobConfig(**kwargs)


def dry_run_estimate(client, config: Dict[str, Any], sql: str) -> Tuple[Optional[int], Optional[str]]:
    if not bool(get_cfg(config, "cost_control.dry_run_before_execute", True)):
        return None, None
    try:
        job = client.query(sql, job_config=make_query_job_config(config, dry_run=True))
        return int(getattr(job, "total_bytes_processed", 0) or 0), None
    except Exception as exc:
        return None, str(exc)


def execute_profile_query(client, config: Dict[str, Any], sql: str, timeout: int) -> Dict[str, Any]:
    job = client.query(sql, job_config=make_query_job_config(config, dry_run=False))
    rows = list(job.result(timeout=timeout))
    return row_to_plain(rows[0]) if rows else {}


def normalize_top_values(raw: Any) -> List[Dict[str, Any]]:
    values = row_to_plain(raw)
    if not values:
        return []
    out: List[Dict[str, Any]] = []
    for item in values:
        if isinstance(item, dict):
            val = item.get("value")
            cnt = item.get("count")
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            val, cnt = item[0], item[1]
        else:
            continue
        out.append({"value": normalize_bq_value(val), "count": int(cnt or 0)})
    return out


def build_column_profile(col: Dict[str, Any], raw: Any) -> Dict[str, Any]:
    stats_raw = row_to_plain(raw) or {}
    stats: Dict[str, Any] = {}
    for key in ["null_count", "distinct_count", "min_value", "max_value", "min_length", "max_length"]:
        if key in stats_raw:
            stats[key] = normalize_bq_value(stats_raw.get(key))
    if "top_values" in stats_raw:
        stats["top_values"] = normalize_top_values(stats_raw.get("top_values"))
    if stats_raw.get("profile_error"):
        stats["profile_error"] = str(stats_raw.get("profile_error"))
    return {**col, "profile_kind": classify_type(col.get("type", "")), "stats": stats}


def skipped_profile(config: Dict[str, Any], table, project_id: str, dataset_id: str, table_name: str, cols: List[Dict[str, Any]], limit: int, top_n: int, profile_window: Dict[str, Any], failures: List[Dict[str, Any]], started: float) -> Dict[str, Any]:
    return {
        "profile_version": PROFILE_VERSION,
        "profiled_at": datetime.now().isoformat(),
        "catalog": project_id,
        "database": dataset_id,
        "schema": dataset_id,
        "name": table_name,
        "object_type": object_type(table),
        "table_type": getattr(table, "table_type", "") or "",
        "description": table.description or "",
        "owner": "",
        "location": f"{project_id}.{dataset_id}.{table_name}",
        "sample_limit": limit,
        "profiled_rows": None,
        "total_table_rows_metadata": getattr(table, "num_rows", None),
        "profile_window": profile_window,
        "top_n": top_n,
        "columns": [{**c, "profile_kind": classify_type(c.get("type", "")), "stats": {}} for c in cols],
        "partitioning": getattr(table.time_partitioning, "field", None) if getattr(table, "time_partitioning", None) else None,
        "clustering": list(table.clustering_fields or []),
        "failures": failures,
        "elapsed_seconds": round(time.time() - started, 2),
    }


def profile_object(client, config: Dict[str, Any], project_id: str, dataset_id: str, table_name: str) -> Dict[str, Any]:
    started = time.time()
    table = client.get_table(f"{project_id}.{dataset_id}.{table_name}")
    is_view = object_type(table) == "VIEW"
    cols = columns_of(table)
    top_n = int(get_cfg(config, "profiling.top_n", 10))
    limit = int(get_cfg(config, "profiling.sample_limit", 10000) or 0)
    timeout = int(get_cfg(config, "profiling.query_timeout_seconds", 900))
    where_filter, profile_window = partition_filter_for_table(config, table, is_view)
    failures: List[Dict[str, Any]] = []

    log(f"Profiling {project_id}.{dataset_id}.{table_name} ({object_type(table)}) columns={len(cols)} sample_limit={limit} window={profile_window}")
    sql = profile_query_sql(project_id, dataset_id, table_name, cols, top_n, limit, where_filter)

    estimated_bytes, dry_run_error = dry_run_estimate(client, config, sql)
    max_bytes = int(get_cfg(config, "cost_control.max_bytes_billed", 0) or 0)
    show_estimated = bool(get_cfg(config, "cost_control.show_estimated_bytes", True))
    stop_if_exceeds = bool(get_cfg(config, "cost_control.stop_if_dry_run_exceeds_limit", True))

    if dry_run_error:
        failures.append({"scope": "dry_run", "error": dry_run_error})
        return skipped_profile(config, table, project_id, dataset_id, table_name, cols, limit, top_n, profile_window, failures, started)

    if estimated_bytes is not None and show_estimated:
        log(f"Dry-run estimated bytes: {estimated_bytes:,}")

    if max_bytes > 0 and estimated_bytes is not None and estimated_bytes > max_bytes and stop_if_exceeds:
        msg = f"Dry-run estimate {estimated_bytes:,} bytes exceeds max_bytes_billed {max_bytes:,}; profiling skipped."
        log(f"[SKIP] {msg}")
        failures.append({"scope": "cost_control", "error": msg, "estimated_bytes": estimated_bytes, "max_bytes_billed": max_bytes})
        return skipped_profile(config, table, project_id, dataset_id, table_name, cols, limit, top_n, profile_window, failures, started)

    try:
        raw_result = execute_profile_query(client, config, sql, timeout)
    except Exception as exc:
        failures.append({"scope": "one_pass_query", "error": str(exc)})
        return skipped_profile(config, table, project_id, dataset_id, table_name, cols, limit, top_n, profile_window, failures, started)

    profiled_rows = int(raw_result.get("__profiled_rows") or 0)
    prof_cols: List[Dict[str, Any]] = []
    for idx, col in enumerate(cols):
        cprof = build_column_profile(col, raw_result.get(f"__col_{idx}"))
        if cprof.get("stats", {}).get("profile_error"):
            failures.append({"scope": f"column:{cprof.get('name')}", "error": cprof["stats"]["profile_error"]})
        prof_cols.append(cprof)

    return {
        "profile_version": PROFILE_VERSION,
        "profiled_at": datetime.now().isoformat(),
        "catalog": project_id,
        "database": dataset_id,
        "schema": dataset_id,
        "name": table_name,
        "object_type": object_type(table),
        "table_type": getattr(table, "table_type", "") or "",
        "description": table.description or "",
        "owner": "",
        "location": f"{project_id}.{dataset_id}.{table_name}",
        "sample_limit": limit,
        "profiled_rows": profiled_rows,
        "total_table_rows_metadata": getattr(table, "num_rows", None),
        "profile_window": profile_window,
        "top_n": top_n,
        "columns": prof_cols,
        "partitioning": getattr(table.time_partitioning, "field", None) if getattr(table, "time_partitioning", None) else None,
        "clustering": list(table.clustering_fields or []),
        "failures": failures,
        "elapsed_seconds": round(time.time() - started, 2),
    }


def save_profile(config: Dict[str, Any], profile: Dict[str, Any]) -> Path:
    out = Path(get_cfg(config, "paths.output_dir"))
    out.mkdir(parents=True, exist_ok=True)
    p = out / f"{safe_name(profile['database'])}.{safe_name(profile['name'])}.profile.json"
    p.write_text(json.dumps(profile, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return p


def profile_many(client, config: Dict[str, Any], project_id: str, objs: List[Dict[str, Any]]) -> int:
    outputs, failures = [], []
    log("Object profiling mode: sequential")
    for i, obj in enumerate(objs, 1):
        ds, name = obj["dataset"], obj["table_id"]
        log(f"Progress {i}/{len(objs)}: {ds}.{name}")
        try:
            path = save_profile(config, profile_object(client, config, project_id, ds, name))
            outputs.append(str(path))
            log(f"Saved: {path}")
        except Exception as exc:
            print(f"[ERROR] Failed {ds}.{name}: {exc}")
            failures.append({"database": ds, "name": name, "error": str(exc)})
    out_dir = Path(get_cfg(config, "paths.output_dir"))
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = out_dir / "_profile_summary.json"
    summary.write_text(
        json.dumps(
            {
                "profiled_at": datetime.now().isoformat(),
                "project_id": project_id,
                "total_requested": len(objs),
                "total_profiles": len(outputs),
                "total_failures": len(failures),
                "outputs": outputs,
                "failures": failures,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    log(f"Summary: {summary}")
    return 2 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Profile BigQuery tables/views into generic JSON profile files.")
    ap.add_argument("--project-config", default=str(DEFAULT_PROJECT_CONFIG_PATH))
    ap.add_argument("--config", default=None)
    ap.add_argument("--list-datasets", action="store_true")
    ap.add_argument("--list-objects", action="store_true")
    ap.add_argument("--dataset", default=None)
    ap.add_argument("--profile", action="store_true")
    ap.add_argument("--name-contains", default="")
    ap.add_argument("--name-starts-with", default="")
    args = ap.parse_args()

    try:
        config_path = Path(args.config) if args.config else resolve_profiler_config_path(Path(args.project_config))
        config = load_json(config_path)
        client, project_id = create_client(config)
    except Exception as exc:
        print(f"[ERROR] Startup failed: {exc}")
        return 1

    if args.list_datasets:
        print("\n".join(default_dataset_scope(client, config, project_id)))
        return 0

    cli_datasets = [args.dataset] if args.dataset else None
    if args.list_objects:
        for o in selected_objects(client, config, project_id, cli_datasets, args.name_contains, args.name_starts_with):
            print(f"{o['dataset']}.{o['table_id']}\t{object_type(o['list_item'])}")
        return 0

    if args.profile:
        objs = selected_objects(client, config, project_id, cli_datasets, args.name_contains, args.name_starts_with)
    else:
        datasets = cli_datasets or choose_datasets(client, config, project_id)
        if not datasets:
            return 0
        print("\nObject filter")
        print("=============")
        print("[1] Show all objects")
        print("[2] Name contains")
        print("[3] Name starts with")
        mode = input("Choose filter mode [1]: ").strip() or "1"
        if mode == "2":
            objs_all = selected_objects(client, config, project_id, datasets, contains=input("Name contains: ").strip())
        elif mode == "3":
            objs_all = selected_objects(client, config, project_id, datasets, starts=input("Name starts with: ").strip())
        else:
            objs_all = selected_objects(client, config, project_id, datasets)
        objs = choose_objects_interactive(objs_all)
        if not objs:
            print("No objects selected.")
            return 0

    max_objects = int(get_cfg(config, "profiling.max_objects_per_run", 10))
    if len(objs) > max_objects:
        print(f"[ERROR] {len(objs)} objects selected; max_objects_per_run={max_objects}.")
        print("       Select fewer objects or increase profiling.max_objects_per_run in the config.")
        return 1
    return profile_many(client, config, project_id, objs)


if __name__ == "__main__":
    sys.exit(main())
