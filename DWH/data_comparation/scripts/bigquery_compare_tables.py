#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from comparison_core import load_yaml, safe_name, save_json, save_yaml, schema_plan
from providers.bigquery_provider import BigQueryProvider

BASE = Path("DWH") / "data_comparation"
CONFIG_DIR = BASE / "config"
RESULT_DIR = BASE / "outputs" / "comparison_results"
REPORT_DIR = BASE / "outputs" / "reports"
VERSION = "provider-architecture-20260713_02"


def ask(label: str, default: str = "") -> str:
    value = input(f"{label}{f' [{default}]' if default else ''}: ").strip()
    return value or default


def yes_no(label: str, default: bool = True) -> bool:
    value = input(f"{label} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not value else value in {"y", "yes", "1"}


def choose(title: str, items: List[Any], show=lambda x: str(x), multi: bool = False):
    if not items:
        print("No items found.")
        return []
    print(f"\n{title}\n{'=' * len(title)}")
    for i, item in enumerate(items, 1):
        print(f"[{i}] {show(item)}")
    raw = input("Selection: ").strip()
    if not raw or raw.lower() in {"q", "quit"}:
        return []
    if multi and raw.lower() == "all":
        return items
    indexes = []
    for part in raw.split(","):
        part = part.strip()
        if "-" in part:
            a, b = [int(x) for x in part.split("-", 1)]
            indexes.extend(range(a, b + 1))
        else:
            indexes.append(int(part))
    return [items[i - 1] for i in indexes]


def choose_object(provider: BigQueryProvider, label: str) -> Dict[str, Any] | None:
    print(f"\nSelect {label} object")
    print("[1] Search name across all datasets")
    print("[2] Browse one dataset")
    mode = ask("Selection", "1")
    if mode == "1":
        text = ask("Object name contains")
        ds_text = ask("Dataset name contains (optional)")
        objects = provider.search_objects(text, ds_text)
    else:
        datasets = choose("Choose dataset", provider.list_namespaces())
        if not datasets:
            return None
        text = ask("Object name contains (optional)")
        objects = [x for x in provider.list_objects(datasets[0]) if text.lower() in x["name"].lower()]
    selected = choose(f"Choose {label} table/view", objects, lambda x: f"{x['namespace']}.{x['name']} | {x['object_type']}")
    return selected[0] if selected else None


def wizard() -> Path | None:
    provider = BigQueryProvider({"project_env": "GCP_PROJECT_ID", "connection": {"env_file": ".env"}})
    left_sel = choose_object(provider, "LEFT")
    right_sel = choose_object(provider, "RIGHT")
    if not left_sel or not right_sel:
        return None
    left = provider.get_object(left_sel["namespace"], left_sel["name"])
    right = provider.get_object(right_sel["namespace"], right_sel["name"])
    ls, rs = {c.name.lower(): c for c in left.columns}, {c.name.lower(): c for c in right.columns}
    common = [ls[k] for k in sorted(ls.keys() & rs.keys()) if ls[k].data_type == rs[k].data_type and ls[k].mode == rs[k].mode]
    keys = choose("Choose unique join key field(s)", common, lambda c: f"{c.name} | {c.data_type} | {c.mode}", multi=True)
    if not keys:
        print("Join key is required.")
        return None
    date_common = [c for c in common if c.data_type in {"DATE", "DATETIME", "TIMESTAMP"}]
    date_filter = {}
    if date_common and yes_no("Apply date filter?", True):
        dc = choose("Choose date field", date_common, lambda c: f"{c.name} | {c.data_type}")
        if dc:
            date_filter = {"left_field": dc[0].name, "right_field": dc[0].name, "from": ask("Date from YYYY-MM-DD"), "to": ask("Date to YYYY-MM-DD")}
    filters = {"left": [], "right": []}
    for side in ("left", "right"):
        if yes_no(f"Add custom {side.upper()} SQL filter?", False):
            val = ask(f"{side.upper()} condition without WHERE")
            if val:
                filters[side].append(val)
    technical = [c.name for c in common if any(x in c.name.lower() for x in ["load", "batch", "etl", "insert", "update", "audit", "run_id"])]
    excluded = technical if technical and yes_no(f"Exclude detected technical columns ({', '.join(technical)})?", True) else []
    default_name = safe_name(f"{left.schema}_{left.name}_vs_{right.schema}_{right.name}")
    name = safe_name(ask("Config name", default_name))
    config = {
        "provider": "bigquery",
        "comparison_mode": "keyed",
        "comparison_name": name,
        "description": ask("Description", f"Compare {left.schema}.{left.name} with {right.schema}.{right.name}"),
        "project_env": "GCP_PROJECT_ID",
        "connection": {"env_file": ".env"},
        "left": {"namespace": left.schema, "object": left.name},
        "right": {"namespace": right.schema, "object": right.name},
        "join_keys": [x.name for x in keys],
        "columns": {"mode": "all_common", "include": [], "exclude": excluded},
        "filters": filters,
        "date_filter": {k: v for k, v in date_filter.items() if v},
        "comparison_options": {
            "numeric_tolerance": {"mode": "absolute", "value": float(ask("Numeric tolerance", "0.001"))},
            "strings": {"trim": False, "case_sensitive": True, "empty_string_equals_null": False},
            "sample": {"max_problem_rows": 10},
            "values": {"show_values": True, "mask_sensitive_values": False},
        },
        "execution": {"query_timeout_seconds": 900},
        "provider_options": {
            "dry_run_before_execute": True,
            "maximum_bytes_billed_gb": float(ask("Maximum bytes billed GB", "25")),
            "stop_if_dry_run_exceeds_limit": True,
        },
        "tags": [x.strip() for x in ask("Tags comma-separated", "validation").split(",") if x.strip()],
    }
    path = CONFIG_DIR / f"{name}.yaml"
    save_yaml(config, path)
    print(f"Saved: {path}")
    if yes_no("Execute now and generate HTML report?", True):
        execute([path], True)
    return path


def make_report(json_path: Path) -> Path:
    script = Path(__file__).parent / "compare_tables_report.py"
    spec = importlib.util.spec_from_file_location("report", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return Path(module.generate_report(json_path, REPORT_DIR))


def execute(paths: List[Path], html: bool) -> int:
    comparisons = []
    for path in paths:
        cfg = load_yaml(path)
        item = {"comparison_name": cfg.get("comparison_name", path.stem), "config_path": str(path), "provider": cfg.get("provider", "bigquery"), "metadata_status": "PASSED", "execution_status": "NOT_STARTED", "errors": []}
        try:
            provider = BigQueryProvider(cfg)
            left_cfg, right_cfg = cfg["left"], cfg["right"]
            left = provider.get_object(left_cfg.get("namespace") or left_cfg.get("dataset"), left_cfg["object"])
            right = provider.get_object(right_cfg.get("namespace") or right_cfg.get("dataset"), right_cfg["object"])
            plan = schema_plan(left, right, cfg)
            item.update({"left_object": f"{left.catalog}.{left.schema}.{left.name}", "right_object": f"{right.catalog}.{right.schema}.{right.name}", "left_object_type": left.object_type, "right_object_type": right.object_type, "join_keys": plan.get("join_keys"), "schema": plan, "schema_status": plan["schema_status"], "key_status": "FAILED" if plan["blockers"] else "PENDING", "data_status": "NOT_RUN"})
            if plan["blockers"]:
                item["execution_status"] = "BLOCKED_SCHEMA_OR_KEY"
                item["errors"] = plan["blockers"]
            else:
                execution = provider.compare(cfg, left, right, plan)
                item["execution_status"] = execution["execution_status"]
                item["estimated_bytes"] = execution.get("estimated_bytes")
                item["max_bytes_billed"] = execution.get("max_bytes_billed")
                item["sqls"] = execution.get("sqls", [])
                result = execution.get("result")
                if result:
                    item["key_validation"] = {k: result.get(k) for k in ["left_null_key_count", "right_null_key_count", "left_duplicate_key_count", "right_duplicate_key_count", "left_duplicate_samples", "right_duplicate_samples"]}
                    key_bad = any(int(result.get(k) or 0) for k in ["left_null_key_count", "right_null_key_count", "left_duplicate_key_count", "right_duplicate_key_count"])
                    item["key_status"] = "FAILED" if key_bad else "PASSED"
                    if key_bad:
                        item["execution_status"] = "BLOCKED_INVALID_KEY"
                        item["data_status"] = "NOT_RUN"
                    else:
                        item["data_result"] = {k: result.get(k) for k in ["left_row_count", "right_row_count", "missing_in_right_count", "missing_in_left_count", "matched_key_count", "rows_with_differences_count", "total_field_differences", "field_difference_counts", "problem_rows_sample"]}
                        total = sum(int(result.get(k) or 0) for k in ["missing_in_right_count", "missing_in_left_count", "total_field_differences"])
                        item["data_result"]["total_differences"] = total
                        item["data_status"] = "PASSED" if total == 0 else "FAILED"
        except Exception as exc:
            item["execution_status"] = "FAILED_QUERY"
            item["errors"].append(str(exc))
        item["finished_at"] = datetime.now(timezone.utc).isoformat()
        if item.get("execution_status") != "COMPLETED" or item.get("key_status") == "FAILED" or item.get("data_status") == "FAILED":
            item["overall_status"] = "FAILED"
        elif item.get("schema_status") == "WARNING":
            item["overall_status"] = "WARNING"
        else:
            item["overall_status"] = "PASSED"
        comparisons.append(item)
    summary = {"total": len(comparisons), "passed": sum(x["overall_status"] == "PASSED" for x in comparisons), "warnings": sum(x["overall_status"] == "WARNING" for x in comparisons), "failed": sum(x["overall_status"] == "FAILED" for x in comparisons)}
    summary["status"] = "FAILED" if summary["failed"] else "WARNING" if summary["warnings"] else "PASSED"
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    result = {"version": VERSION, "run_id": run_id, "generated_at": datetime.now(timezone.utc).isoformat(), "summary": summary, "comparisons": comparisons}
    out = RESULT_DIR / f"comparison_run_{run_id}.json"
    save_json(result, out)
    print(f"JSON: {out}")
    if html:
        print(f"HTML: {make_report(out)}")
    return 2 if summary["failed"] else 0


def saved_configs() -> List[Path]:
    return sorted(list(CONFIG_DIR.glob("*.yaml")) + list(CONFIG_DIR.glob("*.yml")))


def start_menu() -> int:
    print("\nBigQuery Table/View Comparison")
    print("==============================")
    print("[1] Create and optionally run a comparison")
    print("[2] Run saved configuration(s)")
    print("[3] List saved configurations")
    print("[4] Generate report from JSON")
    print("[5] Exit")
    choice = ask("Selection", "1")
    if choice == "1":
        wizard(); return 0
    if choice == "2":
        paths = choose("Choose configs (comma/range/all)", saved_configs(), lambda p: p.name, multi=True)
        return execute(paths, True) if paths else 0
    if choice == "3":
        for p in saved_configs(): print(p)
        return 0
    if choice == "4":
        print(make_report(Path(ask("JSON path"))))
        return 0
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wizard", action="store_true")
    ap.add_argument("--config", action="append", default=[])
    ap.add_argument("--config-dir")
    ap.add_argument("--html-report", action="store_true")
    ap.add_argument("--validate-only", action="store_true")
    args = ap.parse_args()
    if len(sys.argv) == 1:
        return start_menu()
    if args.wizard:
        return 0 if wizard() else 1
    paths = [Path(x) for x in args.config]
    if args.config_dir:
        paths += sorted(Path(args.config_dir).glob("*.yaml"))
    if args.validate_only:
        for p in paths:
            cfg = load_yaml(p)
            print(f"OK: {p}" if cfg.get("left") and cfg.get("right") else f"INVALID: {p}")
        return 0
    return execute(paths, args.html_report)

if __name__ == "__main__":
    raise SystemExit(main())
