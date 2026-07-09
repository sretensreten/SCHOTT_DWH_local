#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, re, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError, PartialCredentialsError
except Exception as exc:
    print("[ERROR] boto3/botocore is not installed. Run: pip install boto3")
    print(f"Details: {exc}")
    sys.exit(1)

DEFAULT_PROJECT_CONFIG_PATH = Path("DWH") / "data_profiling" / "config" / "profiler_project.json"

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
    project_config = json.loads(project_config_path.read_text(encoding="utf-8"))
    config_value = project_config.get("active_profiler_config")
    if not config_value:
        raise ValueError(f"Missing active_profiler_config in {project_config_path}")
    config_path = Path(config_value)
    if config_path.is_absolute():
        return config_path
    if config_path.is_file():
        return config_path
    return project_config_path.parent / config_path


def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_") or "unnamed"

def aws_shared_file_paths() -> tuple[Path, Path]:
    home = Path.home()
    return home / ".aws" / "config", home / ".aws" / "credentials"

def parse_aws_profile_headers(path: Path, is_config_file: bool) -> list[str]:
    if not path.is_file():
        return []
    profiles = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not (line.startswith("[") and line.endswith("]")):
            continue
        section = line[1:-1].strip()
        if is_config_file:
            if section.startswith("profile "):
                section = section[len("profile "):].strip()
            elif section.startswith("sso-session ") or section.startswith("services "):
                continue
        if section:
            profiles.append(section)
    out, seen = [], set()
    for profile in profiles:
        if profile not in seen:
            out.append(profile)
            seen.add(profile)
    return out

def available_aws_profiles() -> list[str]:
    cfg, cred = aws_shared_file_paths()
    out, seen = [], set()
    for profile in parse_aws_profile_headers(cred, False) + parse_aws_profile_headers(cfg, True):
        if profile not in seen:
            out.append(profile)
            seen.add(profile)
    return out

def resolve_aws_profile(configured_profile: Optional[str], interactive: bool = True) -> Optional[str]:
    requested = (configured_profile or "").strip()
    if requested and requested.lower() not in {"auto", "detect"}:
        return requested
    profiles = available_aws_profiles()
    if not profiles:
        return None
    if len(profiles) == 1:
        return profiles[0]
    if not interactive:
        return "default" if "default" in profiles else profiles[0]
    print("Available AWS profiles found in ~/.aws/config and ~/.aws/credentials:")
    for i, p in enumerate(profiles, 1):
        print(f"  [{i}] {p}")
    while True:
        choice = input("Choose AWS profile number: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(profiles):
            return profiles[int(choice) - 1]
        print("Invalid choice.")

def create_session(config: Dict[str, Any]):
    profile = resolve_aws_profile(get_cfg(config, "aws.profile", "auto"), True)
    region = get_cfg(config, "aws.region", None)
    if not profile:
        raise RuntimeError("No AWS profile detected in ~/.aws/config or ~/.aws/credentials")
    log(f"AWS profile: {profile} (configured: {get_cfg(config, 'aws.profile', 'auto')})")
    log(f"AWS region: {region or 'not set'}")
    return boto3.Session(profile_name=profile, region_name=region or None)

def quote_ident(x: str) -> str:
    return '"' + x.replace('"', '""') + '"'

def table_ref(db: str, name: str) -> str:
    return f"{quote_ident(db)}.{quote_ident(name)}"

def classify_type(t: str) -> str:
    v = (t or "").lower().strip()
    if v.startswith("decimal") or v in {"tinyint", "smallint", "integer", "int", "bigint", "real", "float", "double"}:
        return "numeric"
    if v == "date" or v.startswith("timestamp"):
        return "temporal"
    if v in {"string", "boolean", "bool"} or v.startswith("varchar") or v.startswith("char"):
        return "categorical"
    return "other"

def is_string_type(t: str) -> bool:
    v = (t or "").lower().strip()
    return v == "string" or v.startswith("varchar") or v.startswith("char")

def is_date_like_name(name: str) -> bool:
    return "date" in (name or "").lower()

def is_view(t: Dict[str, Any]) -> bool:
    return (t.get("TableType") or "").upper() == "VIRTUAL_VIEW" or bool(t.get("ViewOriginalText"))

def object_type(t: Dict[str, Any]) -> str:
    return "VIEW" if is_view(t) else "TABLE"

def get_databases(glue) -> List[str]:
    out = []
    for page in glue.get_paginator("get_databases").paginate():
        out += [d["Name"] for d in page.get("DatabaseList", []) if d.get("Name")]
    return sorted(out)

def get_tables(glue, db: str) -> List[Dict[str, Any]]:
    out = []
    for page in glue.get_paginator("get_tables").paginate(DatabaseName=db):
        out += page.get("TableList", [])
    return sorted(out, key=lambda x: x.get("Name", ""))

def columns_of(t: Dict[str, Any]) -> List[Dict[str, Any]]:
    out, seen = [], set()
    for c in (t.get("StorageDescriptor", {}) or {}).get("Columns", []) or []:
        name = c.get("Name")
        if name and name.lower() not in seen:
            out.append({"name": name, "type": c.get("Type", ""), "comment": c.get("Comment", ""), "partition_key": False})
            seen.add(name.lower())
    for c in t.get("PartitionKeys", []) or []:
        name = c.get("Name")
        if name and name.lower() not in seen:
            out.append({"name": name, "type": c.get("Type", ""), "comment": c.get("Comment", ""), "partition_key": True})
            seen.add(name.lower())
    return out

def wait_query(athena, qid: str, timeout: int, poll: int) -> Dict[str, Any]:
    started = time.time()
    while True:
        res = athena.get_query_execution(QueryExecutionId=qid)
        state = res["QueryExecution"]["Status"].get("State")
        if state in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return res
        if time.time() - started > timeout:
            raise TimeoutError(f"Athena query timed out after {timeout}s. QueryExecutionId={qid}")
        time.sleep(max(1, poll))

def run_query(athena, config: Dict[str, Any], sql: str, db: Optional[str] = None) -> Tuple[str, List[Dict[str, Any]]]:
    wg = get_cfg(config, "athena.workgroup", "primary")
    catalog = get_cfg(config, "athena.catalog", "AwsDataCatalog")
    output = get_cfg(config, "athena.query_results_s3", "")
    ctx = {"Catalog": catalog}
    if db:
        ctx["Database"] = db
    req = {"QueryString": sql, "WorkGroup": wg, "QueryExecutionContext": ctx}
    if output:
        req["ResultConfiguration"] = {"OutputLocation": output}
    qid = athena.start_query_execution(**req)["QueryExecutionId"]
    final = wait_query(athena, qid, int(get_cfg(config, "profiling.query_timeout_seconds", 900)), int(get_cfg(config, "profiling.poll_interval_seconds", 2)))
    st = final["QueryExecution"]["Status"]
    if st.get("State") != "SUCCEEDED":
        raise RuntimeError(f"Athena query failed: {st.get('StateChangeReason', 'no reason')}\nQueryExecutionId={qid}\nSQL:\n{sql}")
    rows, headers, first = [], [], True
    for page in athena.get_paginator("get_query_results").paginate(QueryExecutionId=qid):
        for r in page.get("ResultSet", {}).get("Rows", []):
            vals = [d.get("VarCharValue") for d in r.get("Data", [])]
            if first:
                headers = vals
                first = False
                continue
            rows.append({headers[i]: vals[i] if i < len(vals) else None for i in range(len(headers))})
    return qid, rows

def source_sql(db: str, name: str, limit: int) -> str:
    base = table_ref(db, name)
    return f"(SELECT * FROM {base} LIMIT {int(limit)})" if limit and limit > 0 else base

def first_row(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    return rows[0] if rows else {}

def profile_column(athena, config: Dict[str, Any], db: str, src: str, col: Dict[str, Any], top_n: int) -> Dict[str, Any]:
    cname, ctype = col["name"], col["type"]
    kind = classify_type(ctype)
    q = quote_ident(cname)
    stats: Dict[str, Any] = {}
    try:
        if kind in {"numeric", "temporal"}:
            sql = f"SELECT SUM(CASE WHEN {q} IS NULL THEN 1 ELSE 0 END) AS null_count, MIN({q}) AS min_value, MAX({q}) AS max_value FROM {src}"
            _, rows = run_query(athena, config, sql, db)
            stats.update(first_row(rows))
        elif is_string_type(ctype) and not is_date_like_name(cname):
            sql = f"SELECT SUM(CASE WHEN {q} IS NULL THEN 1 ELSE 0 END) AS null_count, COUNT(DISTINCT {q}) AS distinct_count, MIN(LENGTH(CAST({q} AS VARCHAR))) AS min_length, MAX(LENGTH(CAST({q} AS VARCHAR))) AS max_length FROM {src}"
            _, rows = run_query(athena, config, sql, db)
            stats.update(first_row(rows))
            top = f"SELECT CAST({q} AS VARCHAR) AS value, COUNT(*) AS count FROM {src} WHERE {q} IS NOT NULL GROUP BY CAST({q} AS VARCHAR) ORDER BY count DESC LIMIT {top_n}"
            _, trs = run_query(athena, config, top, db)
            stats["top_values"] = [{"value": r.get("value"), "count": int(r.get("count") or 0)} for r in trs]
        elif kind == "categorical":
            sql = f"SELECT SUM(CASE WHEN {q} IS NULL THEN 1 ELSE 0 END) AS null_count, COUNT(DISTINCT {q}) AS distinct_count, MIN(CAST({q} AS VARCHAR)) AS min_value, MAX(CAST({q} AS VARCHAR)) AS max_value FROM {src}"
            _, rows = run_query(athena, config, sql, db)
            stats.update(first_row(rows))
            top = f"SELECT CAST({q} AS VARCHAR) AS value, COUNT(*) AS count FROM {src} WHERE {q} IS NOT NULL GROUP BY CAST({q} AS VARCHAR) ORDER BY count DESC LIMIT {top_n}"
            _, trs = run_query(athena, config, top, db)
            stats["top_values"] = [{"value": r.get("value"), "count": int(r.get("count") or 0)} for r in trs]
        else:
            sql = f"SELECT SUM(CASE WHEN {q} IS NULL THEN 1 ELSE 0 END) AS null_count, MIN(CAST({q} AS VARCHAR)) AS min_value, MAX(CAST({q} AS VARCHAR)) AS max_value FROM {src}"
            _, rows = run_query(athena, config, sql, db)
            stats.update(first_row(rows))
    except Exception as exc:
        stats["profile_error"] = str(exc)
    return {**col, "profile_kind": kind, "stats": stats}

def profile_object(athena, config: Dict[str, Any], db: str, table: Dict[str, Any], dbt_info: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    started = time.time()
    name = table["Name"]
    cols = columns_of(table)
    top_n = int(get_cfg(config, "profiling.top_n", 20))
    limit = int(get_cfg(config, "profiling.sample_limit", 100000) or 0)
    src = source_sql(db, name, limit)
    failures = []
    log(f"Profiling {db}.{name} ({object_type(table)}) columns={len(cols)} sample_limit={limit}")
    try:
        _, rows = run_query(athena, config, f"SELECT COUNT(*) AS n FROM {src}", db)
        profiled_rows = int(first_row(rows).get("n") or 0)
    except Exception as exc:
        profiled_rows = None
        failures.append({"scope": "profiled_rows", "error": str(exc)})
    parallel = bool(get_cfg(config, "performance.parallel_columns", True))
    max_workers = max(1, int(get_cfg(config, "performance.max_workers", 4)))
    prof_cols: List[Optional[Dict[str, Any]]] = [None] * len(cols)
    if parallel and len(cols) > 1:
        log(f"Column profiling mode: parallel, max_workers={max_workers}")
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            fmap = {ex.submit(profile_column, athena, config, db, src, c, top_n): i for i, c in enumerate(cols)}
            for fut in as_completed(fmap):
                i = fmap[fut]
                try:
                    prof_cols[i] = fut.result()
                except Exception as exc:
                    c = cols[i]
                    prof_cols[i] = {**c, "profile_kind": classify_type(c.get("type", "")), "stats": {"profile_error": str(exc)}}
    else:
        log("Column profiling mode: sequential")
        for i, c in enumerate(cols):
            prof_cols[i] = profile_column(athena, config, db, src, c, top_n)
    for c in prof_cols:
        if c and (c.get("stats") or {}).get("profile_error"):
            failures.append({"scope": f"column:{c.get('name')}", "error": c["stats"]["profile_error"]})
    sd = table.get("StorageDescriptor", {}) or {}
    profile = {"profile_version": "athena-1.4", "profiled_at": datetime.now().isoformat(), "catalog": get_cfg(config, "athena.catalog", "AwsDataCatalog"), "database": db, "name": name, "object_type": object_type(table), "table_type": table.get("TableType", ""), "description": table.get("Description", ""), "owner": table.get("Owner", ""), "location": sd.get("Location", ""), "sample_limit": limit, "profiled_rows": profiled_rows, "top_n": top_n, "columns": [c for c in prof_cols if c], "parameters": table.get("Parameters", {}) or {}, "view_original_text_present": bool(table.get("ViewOriginalText")), "view_expanded_text_present": bool(table.get("ViewExpandedText")), "failures": failures, "elapsed_seconds": round(time.time() - started, 2)}
    if dbt_info:
        profile["dbt"] = dbt_info
    return profile

# --- DBT TAG FILTERING: isolated addition ---
def load_dbt_manifest(config: Dict[str, Any]) -> Dict[str, Any]:
    manifest_path = Path(get_cfg(config, "paths.dbt_manifest_path", ""))
    if not manifest_path.is_file():
        raise FileNotFoundError(f"dbt manifest.json not found: {manifest_path}")
    return load_json(manifest_path)

def dbt_tags_from_manifest(manifest: Dict[str, Any]) -> List[str]:
    tags = set()
    for node in (manifest.get("nodes") or {}).values():
        if node.get("resource_type") != "model":
            continue
        for tag in (node.get("tags") or []):
            tags.add(str(tag))
        for tag in ((node.get("config") or {}).get("tags") or []):
            tags.add(str(tag))
    return sorted(tags, key=str.lower)

def dbt_model_candidates(manifest: Dict[str, Any], wanted_tags: List[str], match_mode: str, database_filter: Optional[str]) -> List[Dict[str, Any]]:
    wanted = {tag.strip().lower() for tag in wanted_tags if tag.strip()}
    out = []
    for node in (manifest.get("nodes") or {}).values():
        if node.get("resource_type") != "model":
            continue
        node_tags = {str(t).lower() for t in (node.get("tags") or [])}
        node_tags |= {str(t).lower() for t in ((node.get("config") or {}).get("tags") or [])}
        if wanted:
            if match_mode == "all" and not wanted.issubset(node_tags):
                continue
            if match_mode != "all" and not (wanted & node_tags):
                continue
        schema = node.get("schema")
        name = node.get("alias") or node.get("name")
        if not schema or not name:
            relation = (node.get("relation_name") or "").replace('"', '').replace('`', '')
            parts = [p for p in relation.split(".") if p]
            if len(parts) >= 2:
                schema = schema or parts[-2]
                name = name or parts[-1]
        if not schema or not name:
            continue
        if database_filter and schema.lower() != database_filter.lower():
            continue
        out.append({"database": schema, "name": name, "unique_id": node.get("unique_id"), "tags": sorted(node_tags), "materialized": (node.get("config") or {}).get("materialized"), "original_file_path": node.get("original_file_path")})
    return sorted(out, key=lambda x: (x["database"].lower(), x["name"].lower()))

def find_glue_table(glue, database: str, table_name: str) -> Optional[Dict[str, Any]]:
    for table in get_tables(glue, database):
        if (table.get("Name") or "").lower() == table_name.lower():
            return table
    return None

def selected_objects_by_dbt_tags(glue, config: Dict[str, Any], tags: List[str], db_filter: Optional[str], match_mode: str) -> List[Dict[str, Any]]:
    manifest = load_dbt_manifest(config)
    candidates = dbt_model_candidates(manifest, tags, match_mode, db_filter)
    if not candidates:
        print("No dbt models matched the selected tag filter.")
        return []
    print(f"DBT tag filter matched {len(candidates)} model(s). Resolving in Glue...")
    out, missing = [], []
    for model in candidates:
        try:
            table = find_glue_table(glue, model["database"], model["name"])
            if table:
                out.append({"database": model["database"], "table": table, "dbt": model})
            else:
                missing.append(f"{model['database']}.{model['name']}")
        except ClientError as exc:
            print(f"[WARN] Cannot list Glue tables for {model['database']}: {exc.response.get('Error', {}).get('Message', str(exc))}")
    if missing:
        print("[WARN] DBT models not found in Glue:")
        for item in missing[:50]:
            print(f"  - {item}")
        if len(missing) > 50:
            print(f"  ... and {len(missing) - 50} more")
    return out
# --- END DBT TAG FILTERING ---

def selected_objects(glue, config: Dict[str, Any], db: Optional[str], contains: str = "", starts: str = "") -> List[Dict[str, Any]]:
    if not db:
        raise ValueError("Database is required")
    inc_t = bool(get_cfg(config, "profiling.include_tables", True))
    inc_v = bool(get_cfg(config, "profiling.include_views", True))
    out = []
    for t in get_tables(glue, db):
        typ, name = object_type(t), t.get("Name", "")
        if typ == "TABLE" and not inc_t:
            continue
        if typ == "VIEW" and not inc_v:
            continue
        if contains and contains.lower() not in name.lower():
            continue
        if starts and not name.lower().startswith(starts.lower()):
            continue
        out.append({"database": db, "table": t})
    return out

def choose(title: str, values: List[str]) -> Optional[str]:
    print("\n" + title)
    print("=" * len(title))
    for i, v in enumerate(values, 1):
        print(f"[{i}] {v}")
    while True:
        c = input("Choose number, or q: ").strip().lower()
        if c in {"q", "quit", "exit"}:
            return None
        if c.isdigit() and 1 <= int(c) <= len(values):
            return values[int(c) - 1]
        print("Invalid choice.")

def save_profile(config: Dict[str, Any], profile: Dict[str, Any]) -> Path:
    out_value = get_cfg(config, "paths.output_dir")
    if not out_value:
        raise ValueError("Missing paths.output_dir in profiler config.")
    out = Path(out_value)
    out.mkdir(parents=True, exist_ok=True)
    p = out / f"{safe_name(profile['database'])}.{safe_name(profile['name'])}.profile.json"
    p.write_text(json.dumps(profile, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return p

def profile_many(config: Dict[str, Any], athena, objs: List[Dict[str, Any]]) -> int:
    outputs, failures = [], []
    for i, obj in enumerate(objs, 1):
        db, table = obj["database"], obj["table"]
        log(f"Progress {i}/{len(objs)}: {db}.{table.get('Name')}")
        try:
            profile = profile_object(athena, config, db, table, obj.get("dbt"))
            path = save_profile(config, profile)
            outputs.append(str(path))
            log(f"Saved: {path}")
        except Exception as exc:
            print(f"[ERROR] Failed {db}.{table.get('Name')}: {exc}")
            failures.append({"database": db, "name": table.get("Name"), "error": str(exc)})
    out_dir_value = get_cfg(config, "paths.output_dir")
    if not out_dir_value:
        raise ValueError("Missing paths.output_dir in profiler config.")
    out_dir = Path(out_dir_value)
    out_dir.mkdir(parents=True, exist_ok=True)
    sp = out_dir / "_profile_summary.json"
    sp.write_text(json.dumps({"profiled_at": datetime.now().isoformat(), "total_requested": len(objs), "total_profiles": len(outputs), "total_failures": len(failures), "outputs": outputs, "failures": failures}, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"Summary: {sp}")
    return 2 if failures else 0

def parse_csv(value: str) -> List[str]:
    return [x.strip() for x in (value or "").split(",") if x.strip()]

def main() -> int:
    ap = argparse.ArgumentParser(description="Profile Athena tables/views into generic JSON profile files.")
    ap.add_argument("--project-config", default=str(DEFAULT_PROJECT_CONFIG_PATH))
    ap.add_argument("--config", default=None)
    ap.add_argument("--list-databases", action="store_true")
    ap.add_argument("--list-objects", action="store_true")
    ap.add_argument("--list-dbt-tags", action="store_true")
    ap.add_argument("--database", default=None, help="Athena/Glue database. Also limits dbt tag selection when used with --dbt-tags.")
    ap.add_argument("--profile", action="store_true")
    ap.add_argument("--name-contains", default="")
    ap.add_argument("--name-starts-with", default="")
    ap.add_argument("--dbt-tags", default="", help="Comma-separated dbt tags from manifest.json.")
    ap.add_argument("--dbt-tag-match", choices=["any", "all"], default=None, help="Default from config dbt.tag_match_mode or 'any'.")
    args = ap.parse_args()
    try:
        config_path = Path(args.config) if args.config else resolve_profiler_config_path(Path(args.project_config))
        config = load_json(config_path)
        session = create_session(config)
        glue = session.client("glue")
        athena = session.client("athena")
    except (NoCredentialsError, PartialCredentialsError):
        print("[ERROR] AWS credentials are missing/incomplete.")
        return 1
    except Exception as exc:
        print(f"[ERROR] Startup failed: {exc}")
        return 1
    if args.list_databases:
        print("\n".join(get_databases(glue)))
        return 0
    if args.list_dbt_tags:
        try:
            tags = dbt_tags_from_manifest(load_dbt_manifest(config))
        except Exception as exc:
            print(f"[ERROR] Cannot read dbt manifest: {exc}")
            return 1
        print("DBT tags found in manifest:" if tags else "No dbt tags found in manifest.")
        for tag in tags:
            print(f"- {tag}")
        return 0
    if args.list_objects:
        if not args.database:
            print("[ERROR] --database is required")
            return 1
        for t in selected_objects(glue, config, args.database, args.name_contains, args.name_starts_with):
            print(f"{args.database}.{t['table'].get('Name')}\t{object_type(t['table'])}")
        return 0
    match_mode = args.dbt_tag_match or get_cfg(config, "dbt.tag_match_mode", "any")
    if args.profile:
        tags = parse_csv(args.dbt_tags)
        if tags:
            objs = selected_objects_by_dbt_tags(glue, config, tags, args.database, match_mode)
        else:
            objs = selected_objects(glue, config, args.database, args.name_contains, args.name_starts_with)
    else:
        db = choose("Choose Athena/Glue database", get_databases(glue))
        if not db:
            return 0
        print("\nFilter mode\n===========\n[1] All objects\n[2] Name contains\n[3] Name starts with\n[4] DBT tag(s)")
        mode = input("Choose filter mode [1]: ").strip() or "1"
        if mode == "2":
            objs = selected_objects(glue, config, db, contains=input("Name contains: ").strip())
        elif mode == "3":
            objs = selected_objects(glue, config, db, starts=input("Name starts with: ").strip())
        elif mode == "4":
            try:
                available_tags = dbt_tags_from_manifest(load_dbt_manifest(config))
                if available_tags:
                    print("\nAvailable dbt tags:")
                    for tag in available_tags:
                        print(f"- {tag}")
            except Exception as exc:
                print(f"[ERROR] Cannot read dbt manifest: {exc}")
                return 1
            tags = parse_csv(input("DBT tag(s), comma separated: ").strip())
            if not tags:
                print("No dbt tags entered.")
                return 0
            mode_choice = input(f"Match mode [any/all] ({match_mode}): ").strip().lower()
            if mode_choice in {"any", "all"}:
                match_mode = mode_choice
            objs = selected_objects_by_dbt_tags(glue, config, tags, db, match_mode)
        else:
            objs = selected_objects(glue, config, db)
        for i, o in enumerate(objs, 1):
            dbt_mark = " | dbt" if o.get("dbt") else ""
            print(f"[{i}] {o['database']}.{o['table'].get('Name')} | {object_type(o['table'])}{dbt_mark}")
        if not objs or input(f"Proceed with {len(objs)} object(s)? [y/N]: ").strip().lower() not in {"y", "yes"}:
            return 0
    max_objects = int(get_cfg(config, "profiling.max_objects_per_run", 50))
    if len(objs) > max_objects:
        print(f"[ERROR] {len(objs)} objects selected; max_objects_per_run={max_objects}.")
        return 1
    if not objs:
        print("No objects selected.")
        return 0
    return profile_many(config, athena, objs)

if __name__ == "__main__":
    sys.exit(main())
