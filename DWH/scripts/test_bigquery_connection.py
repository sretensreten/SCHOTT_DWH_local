#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import boto3
    from botocore.exceptions import (
        ClientError,
        EndpointConnectionError,
        NoCredentialsError,
        PartialCredentialsError,
        ProfileNotFound,
    )
except Exception as exc:
    print("[ERROR] boto3/botocore is not installed. Run: pip install boto3")
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


def line() -> None:
    print("-" * 72)


def get_cfg(config: Dict[str, Any], path: str, default: Any = None) -> Any:
    cur: Any = config
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def load_config(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Config file not found: {path}")
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



def aws_shared_file_paths() -> tuple[Path, Path]:
    home = Path.home()
    return home / ".aws" / "config", home / ".aws" / "credentials"


def parse_aws_profile_headers(path: Path, is_config_file: bool) -> list[str]:
    if not path.is_file():
        return []
    profiles: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line.startswith("[") or not line.endswith("]"):
            continue
        section = line[1:-1].strip()
        if is_config_file:
            if section.startswith("profile "):
                section = section[len("profile "):].strip()
            elif section.startswith("sso-session ") or section.startswith("services "):
                continue
        if section:
            profiles.append(section)
    seen, out = set(), []
    for profile in profiles:
        if profile not in seen:
            out.append(profile)
            seen.add(profile)
    return out


def available_aws_profiles() -> list[str]:
    cfg, cred = aws_shared_file_paths()
    discovered = parse_aws_profile_headers(cred, False) + parse_aws_profile_headers(cfg, True)
    seen, out = set(), []
    for profile in discovered:
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
    for index, profile in enumerate(profiles, 1):
        print(f"  [{index}] {profile}")
    while True:
        choice = input("Choose AWS profile number: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(profiles):
            return profiles[int(choice) - 1]
        print("Invalid choice.")


def client_error_summary(exc: ClientError) -> str:
    code = exc.response.get("Error", {}).get("Code", "Unknown")
    message = exc.response.get("Error", {}).get("Message", str(exc))
    return f"{code}: {message}"


def wait_athena(athena, qid: str, timeout: int, poll: int) -> Dict[str, Any]:
    started = time.time()
    while True:
        res = athena.get_query_execution(QueryExecutionId=qid)
        state = res["QueryExecution"]["Status"].get("State")
        if state in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return res
        if time.time() - started > timeout:
            raise TimeoutError(f"Athena query timed out after {timeout}s. QueryExecutionId={qid}")
        time.sleep(max(1, poll))


def test_sts(session) -> bool:
    try:
        identity = session.client("sts").get_caller_identity()
        ok("STS identity check successful")
        print(f"     Account: {identity.get('Account')}")
        print(f"     ARN    : {identity.get('Arn')}")
        return True
    except (NoCredentialsError, PartialCredentialsError):
        err("AWS credentials are missing or incomplete.")
        return False
    except ClientError as exc:
        err("STS identity check failed", client_error_summary(exc))
        return False
    except Exception as exc:
        err("Unexpected STS error", str(exc))
        return False


def get_databases_safe(glue) -> tuple[bool, List[str]]:
    try:
        dbs: List[str] = []
        for page in glue.get_paginator("get_databases").paginate():
            dbs.extend([d.get("Name") for d in page.get("DatabaseList", []) if d.get("Name")])
        ok("Glue database listing successful", f"Databases visible: {len(dbs)}")
        return True, sorted(dbs)
    except ClientError as exc:
        err("Glue database listing failed", client_error_summary(exc))
        return False, []
    except Exception as exc:
        err("Unexpected Glue database listing error", str(exc))
        return False, []


def test_glue_get_tables(glue, databases: List[str], requested_database: Optional[str]) -> bool:
    if requested_database:
        databases_to_test = [requested_database]
    else:
        databases_to_test = databases

    if not databases_to_test:
        warn("No Glue databases available for glue:GetTables test")
        return True

    print("Glue table access test:")
    all_ok = True
    for database in databases_to_test:
        try:
            paginator = glue.get_paginator("get_tables")
            page_iterator = paginator.paginate(
                DatabaseName=database,
                PaginationConfig={"MaxItems": 1, "PageSize": 1},
            )
            first_page = next(iter(page_iterator), {})
            table_count_first_page = len(first_page.get("TableList", []))
            ok(f"glue:GetTables allowed for database: {database}", f"First page tables: {table_count_first_page}")
        except ClientError as exc:
            all_ok = False
            err(f"glue:GetTables denied/failed for database: {database}", client_error_summary(exc))
        except Exception as exc:
            all_ok = False
            err(f"Unexpected glue:GetTables error for database: {database}", str(exc))
    return all_ok


def test_athena(session, config: Dict[str, Any], run_query: bool) -> bool:
    workgroup = get_cfg(config, "athena.workgroup", "primary")
    catalog = get_cfg(config, "athena.catalog", "AwsDataCatalog")
    output_s3 = get_cfg(config, "athena.query_results_s3", "")
    timeout = int(get_cfg(config, "profiling.query_timeout_seconds", 900))
    poll = int(get_cfg(config, "profiling.poll_interval_seconds", 2))
    try:
        athena = session.client("athena")
        athena.get_work_group(WorkGroup=workgroup)
        ok("Athena workgroup found", workgroup)
        if output_s3:
            ok("Config Athena output location", output_s3)
        else:
            warn("No query output S3 path found", "Set athena.query_results_s3 if SELECT 1 fails.")
        if not run_query:
            return True
        request = {
            "QueryString": "SELECT 1 AS connection_test",
            "WorkGroup": workgroup,
            "QueryExecutionContext": {"Catalog": catalog},
        }
        if output_s3:
            request["ResultConfiguration"] = {"OutputLocation": output_s3}
        qid = athena.start_query_execution(**request)["QueryExecutionId"]
        final = wait_athena(athena, qid, timeout, poll)
        state = final["QueryExecution"]["Status"].get("State")
        if state == "SUCCEEDED":
            ok("Athena test query successful", f"QueryExecutionId={qid}")
            return True
        reason = final["QueryExecution"]["Status"].get("StateChangeReason", "No reason returned")
        err("Athena test query failed", reason)
        return False
    except ClientError as exc:
        err("Athena access check failed", client_error_summary(exc))
        return False
    except Exception as exc:
        err("Unexpected Athena error", str(exc))
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Test AWS/Athena/Glue connectivity for data profiling.")
    parser.add_argument("--project-config", default=str(DEFAULT_PROJECT_CONFIG_PATH))
    parser.add_argument("--config", default=None)
    parser.add_argument("--database", default=None, help="Optional Glue database to test with glue:GetTables.")
    parser.add_argument("--skip-athena-query", action="store_true")
    args = parser.parse_args()

    print("=" * 72)
    print("AWS / ATHENA / GLUE CONNECTION TEST")
    print("=" * 72)

    try:
        config_path = Path(args.config) if args.config else resolve_profiler_config_path(Path(args.project_config))
        config = load_config(config_path)
        ok("Config loaded", str(config_path))
    except Exception as exc:
        err("Config load failed", str(exc))
        return 1

    configured_profile = get_cfg(config, "aws.profile", "auto")
    region = get_cfg(config, "aws.region", "")
    profile = resolve_aws_profile(configured_profile, interactive=True)
    if not profile:
        err("No AWS profile detected in ~/.aws/config or ~/.aws/credentials")
        return 1

    ok("AWS profile", f"{profile} (configured: {configured_profile})")
    ok("AWS region", region or "not set")

    try:
        session = boto3.Session(profile_name=profile, region_name=region or None)
        ok("Boto3 session initialized")
    except ProfileNotFound:
        err("AWS profile was not found by Boto3", profile)
        return 1
    except Exception as exc:
        err("Boto3 session initialization failed", str(exc))
        return 1

    line()
    sts_ok = test_sts(session)

    line()
    try:
        glue = session.client("glue")
    except EndpointConnectionError as exc:
        err("Cannot reach Glue endpoint", str(exc))
        return 2

    db_list_ok, databases = get_databases_safe(glue)
    glue_tables_ok = False
    if db_list_ok:
        glue_tables_ok = test_glue_get_tables(glue, databases, args.database)

    line()
    athena_ok = test_athena(session, config, run_query=not args.skip_athena_query)

    line()
    print("SUMMARY")
    print(f"  STS identity      : {'OK' if sts_ok else 'FAILED'}")
    print(f"  Glue databases    : {'OK' if db_list_ok else 'FAILED'}")
    print(f"  Glue GetTables    : {'OK' if glue_tables_ok else 'FAILED'}")
    print(f"  Athena query      : {'OK' if athena_ok else 'FAILED'}")

    if sts_ok and db_list_ok and glue_tables_ok and athena_ok:
        print("\nRESULT: Connection test PASSED.")
        return 0

    print("\nRESULT: Connection test FAILED.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
