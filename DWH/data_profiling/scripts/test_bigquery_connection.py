#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict

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


def ok(msg: str, detail: str = "") -> None:
    print(f"[OK] {msg}")
    if detail:
        print(f"     {detail}")


def warn(msg: str, detail: str = "") -> None:
    print(f"[WARN] {msg}")
    if detail:
        print(f"       {detail}")


def err(msg: str, detail: str = "") -> None:
    print(f"[ERROR] {msg}")
    if detail:
        print(f"        {detail}")


def get_cfg(config: Dict[str, Any], path: str, default: Any = None) -> Any:
    cur: Any = config
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def load_json(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Config file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_profiler_config_path(project_config_path: Path) -> Path:
    if not project_config_path.is_file():
        raise FileNotFoundError(f"Profiler project config not found: {project_config_path}")
    config_value = load_json(project_config_path).get("active_profiler_config")
    if not config_value:
        raise ValueError(f"Missing active_profiler_config in {project_config_path}")
    p = Path(config_value)
    if p.is_absolute() or p.is_file():
        return p
    return project_config_path.parent / p


def load_env(config: Dict[str, Any]) -> None:
    if load_dotenv is None:
        warn("python-dotenv not installed", "Skipping .env loading.")
        return
    env_file = get_cfg(config, "connection.env_file", ".env")
    for p in [Path(env_file), Path.cwd() / ".env", Path.cwd() / ".test" / ".env"]:
        if p and p.is_file():
            load_dotenv(p)
            ok("Env file loaded", str(p))
            return
    warn("No .env file found", "Using existing environment / ADC.")


def resolve_project_id(config: Dict[str, Any], override: str | None = None) -> str:
    value = override or get_cfg(config, "bigquery.project_id") or os.getenv(get_cfg(config, "connection.project_id_env", "GCP_PROJECT_ID")) or os.getenv("GOOGLE_CLOUD_PROJECT")
    if not value:
        raise ValueError("Missing project id. Set bigquery.project_id, GCP_PROJECT_ID, GOOGLE_CLOUD_PROJECT, or pass --project.")
    return str(value).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Test BigQuery connectivity for data profiling.")
    parser.add_argument("--project-config", default=str(DEFAULT_PROJECT_CONFIG_PATH))
    parser.add_argument("--config", default=None)
    parser.add_argument("--project", default=None)
    parser.add_argument("--dataset", default=None)
    parser.add_argument("--list-datasets", action="store_true")
    args = parser.parse_args()

    print("=" * 72)
    print("BIGQUERY CONNECTION TEST")
    print("=" * 72)

    try:
        config_path = Path(args.config) if args.config else resolve_profiler_config_path(Path(args.project_config))
        config = load_json(config_path)
        ok("Config loaded", str(config_path))
        load_env(config)
        project_id = resolve_project_id(config, args.project)
    except Exception as exc:
        err("Config/environment load failed", str(exc))
        return 1

    try:
        client = bigquery.Client(project=project_id, location=get_cfg(config, "bigquery.location") or None)
        ok("BigQuery client initialized", f"client.project={client.project}")
    except Exception as exc:
        err("BigQuery client initialization failed", str(exc))
        return 1

    try:
        rows = list(client.query("SELECT CURRENT_TIMESTAMP() AS current_time, SESSION_USER() AS current_user").result(timeout=60))
        ok("BigQuery test query successful")
        if rows:
            print(f"     Current time : {rows[0].current_time}")
            print(f"     Current user : {rows[0].current_user}")
    except Exception as exc:
        err("BigQuery test query failed", str(exc))
        return 2

    try:
        datasets = list(client.list_datasets(project=project_id))
        ok("Dataset listing successful", f"Datasets visible: {len(datasets)}")
        if args.list_datasets:
            for ds in sorted(datasets, key=lambda d: d.dataset_id):
                print(f"- {ds.dataset_id}")
    except Exception as exc:
        err("Dataset listing failed", str(exc))
        return 2

    if args.dataset:
        try:
            tables = list(client.list_tables(f"{project_id}.{args.dataset}", max_results=1))
            ok("Table listing successful", f"Dataset={args.dataset}; first-page tables={len(tables)}")
        except Exception as exc:
            err("Dataset/table access failed", str(exc))
            return 2

    print("RESULT: BigQuery connection test PASSED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
