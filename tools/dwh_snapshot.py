from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List

SNAPSHOT_VERSION = "schott_dwh_snapshot_v1"

DWH_ROOTS = ["DWH"]
PROJECT_FILES = [
    "AGENTS.md",
    "README.md",
    "package.json",
    ".gitignore",
    ".vscode/tasks.json",
    ".runhub.project/project.json",
    ".runhub.project/actions.json",
    ".runhub.project/categories.json",
]

EXCLUDED_DIR_NAMES = {
    ".git",
    ".venv",
    ".venv-linux",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    "outputs",
    "backups",
    "snapshot",
    "context_bundle",
    "runs",
}

TEXT_SUFFIXES = {".py", ".ps1", ".bat", ".cmd", ".sh", ".json", ".yml", ".yaml", ".md", ".txt"}

FORBIDDEN_IMPORT_PREFIXES = ["DA", "DA.", "agent_eval", "agent_eval."]


def read_text_safe(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8-sig", errors="replace")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def iter_files(root: Path, rel_root: str) -> Iterable[Path]:
    start = root / rel_root
    if not start.exists():
        return []
    files: List[Path] = []
    for current, dirs, names in os.walk(start):
        dirs[:] = [d for d in dirs if d not in EXCLUDED_DIR_NAMES]
        for name in sorted(names):
            p = Path(current) / name
            if p.is_file():
                files.append(p)
    return files


def collect_files(root: Path) -> List[Path]:
    files: List[Path] = []
    for rel in DWH_ROOTS:
        files.extend(iter_files(root, rel))
    for rel in PROJECT_FILES:
        p = root / rel
        if p.is_file():
            files.append(p)
    return sorted(set(files), key=lambda p: str(p).lower())


def parse_python_imports(path: Path) -> Dict[str, Any]:
    imports: List[str] = []
    parse_error = None
    try:
        tree = ast.parse(read_text_safe(path), filename=str(path))
    except Exception as exc:
        return {"imports": [], "parse_error": str(exc), "forbidden_imports": []}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)

    forbidden = []
    for item in sorted(set(imports)):
        if any(item == prefix.rstrip(".") or item.startswith(prefix) for prefix in FORBIDDEN_IMPORT_PREFIXES):
            forbidden.append(item)

    return {
        "imports": sorted(set(imports)),
        "parse_error": parse_error,
        "forbidden_imports": forbidden,
    }


def exported_symbols(path: Path) -> Dict[str, List[str]]:
    if path.suffix.lower() != ".py":
        return {"functions": [], "classes": [], "constants": []}
    try:
        tree = ast.parse(read_text_safe(path), filename=str(path))
    except Exception:
        return {"functions": [], "classes": [], "constants": []}

    functions: List[str] = []
    classes: List[str] = []
    constants: List[str] = []
    for node in tree.body if isinstance(tree, ast.Module) else []:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            functions.append(node.name)
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            classes.append(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    constants.append(target.id)

    return {
        "functions": sorted(set(functions)),
        "classes": sorted(set(classes)),
        "constants": sorted(set(constants)),
    }


def validate_json(path: Path) -> str | None:
    try:
        json.loads(read_text_safe(path))
        return None
    except Exception as exc:
        return str(exc)


def build_snapshot(root: Path) -> Dict[str, Any]:
    files = collect_files(root)
    file_rows: List[Dict[str, Any]] = []
    forbidden_import_findings: List[Dict[str, Any]] = []
    json_errors: List[Dict[str, Any]] = []

    for path in files:
        rel = path.relative_to(root).as_posix()
        suffix = path.suffix.lower()
        try:
            line_count = len(read_text_safe(path).splitlines()) if suffix in TEXT_SUFFIXES else None
        except Exception:
            line_count = None

        row: Dict[str, Any] = {
            "file": rel,
            "suffix": suffix,
            "bytes": path.stat().st_size,
            "lines": line_count,
            "sha256": sha256_file(path)[:12],
        }

        if suffix == ".py":
            import_info = parse_python_imports(path)
            row["python_imports"] = import_info
            row["exports"] = exported_symbols(path)
            if import_info["forbidden_imports"]:
                forbidden_import_findings.append({
                    "file": rel,
                    "forbidden_imports": import_info["forbidden_imports"],
                })

        if suffix == ".json":
            err = validate_json(path)
            if err:
                json_errors.append({"file": rel, "error": err})

        file_rows.append(row)

    missing_project_files = [rel for rel in PROJECT_FILES if not (root / rel).is_file()]
    suffix_counter = Counter(row["suffix"] or "<none>" for row in file_rows)

    status = "PASS" if not forbidden_import_findings and not json_errors else "FAIL"

    return {
        "snapshot_version": SNAPSHOT_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "root": str(root),
        "status": status,
        "summary": {
            "file_count": len(file_rows),
            "python_file_count": suffix_counter.get(".py", 0),
            "forbidden_import_finding_count": len(forbidden_import_findings),
            "json_error_count": len(json_errors),
            "missing_project_file_count": len(missing_project_files),
            "by_suffix": dict(sorted(suffix_counter.items())),
        },
        "missing_project_files": missing_project_files,
        "forbidden_import_findings": forbidden_import_findings,
        "json_errors": json_errors,
        "files": file_rows,
    }


def markdown_report(snapshot: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append("# SCHOTT DWH Snapshot")
    lines.append("")
    lines.append(f"Generated: `{snapshot['generated_at']}`")
    lines.append(f"Snapshot version: `{snapshot['snapshot_version']}`")
    lines.append(f"Root: `{snapshot['root']}`")
    lines.append(f"Status: `{snapshot['status']}`")
    lines.append("")

    lines.append("## Summary")
    lines.append("")
    for key, value in snapshot["summary"].items():
        lines.append(f"- {key}: `{value}`")
    lines.append("")

    if snapshot["missing_project_files"]:
        lines.append("## Missing Project Files")
        lines.append("")
        for rel in snapshot["missing_project_files"]:
            lines.append(f"- ⚠️ `{rel}`")
        lines.append("")

    if snapshot["json_errors"]:
        lines.append("## JSON Errors")
        lines.append("")
        for item in snapshot["json_errors"]:
            lines.append(f"- ❌ `{item['file']}`: {item['error']}")
        lines.append("")

    if snapshot["forbidden_import_findings"]:
        lines.append("## Forbidden Imports")
        lines.append("")
        for item in snapshot["forbidden_import_findings"]:
            lines.append(f"### `{item['file']}`")
            for imp in item["forbidden_imports"]:
                lines.append(f"- ❌ `{imp}`")
            lines.append("")

    lines.append("## Python Exports")
    lines.append("")
    for row in snapshot["files"]:
        if row.get("suffix") != ".py":
            continue
        exports = row.get("exports", {})
        functions = exports.get("functions", [])
        classes = exports.get("classes", [])
        constants = exports.get("constants", [])
        lines.append(f"### `{row['file']}`")
        lines.append(f"- Functions: `{', '.join(functions) or '<none>'}`")
        lines.append(f"- Classes: `{', '.join(classes) or '<none>'}`")
        lines.append(f"- Constants: `{', '.join(constants) or '<none>'}`")
        lines.append("")

    lines.append("## File Index")
    lines.append("")
    for row in snapshot["files"]:
        lines.append(f"- `{row['file']}` lines=`{row['lines']}` bytes=`{row['bytes']}` sha256=`{row['sha256']}`")
    lines.append("")

    lines.append("## Safety Note")
    lines.append("")
    lines.append("This snapshot is inspection-only. It does not modify DWH files or runtime behavior.")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Snapshot and validate SCHOTT_DWH_local project files.")
    parser.add_argument("--root", default=".", help="Project root. Defaults to current directory.")
    parser.add_argument("--output-dir", default="snapshot/dwh", help="Output directory relative to root.")
    parser.add_argument("--validate-only", action="store_true", help="Only validate and print status; do not write snapshot files.")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    snapshot = build_snapshot(root)

    if not args.validate_only:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = (root / args.output_dir).resolve()
        latest_dir = output_dir / "latest"
        runs_dir = output_dir / "runs"
        latest_dir.mkdir(parents=True, exist_ok=True)
        runs_dir.mkdir(parents=True, exist_ok=True)

        latest_json = latest_dir / "dwh_snapshot_latest.json"
        run_json = runs_dir / f"dwh_snapshot_{stamp}.json"
        latest_md = latest_dir / "dwh_snapshot_latest.md"
        run_md = runs_dir / f"dwh_snapshot_{stamp}.md"

        latest_json.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")
        run_json.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")
        md = markdown_report(snapshot)
        latest_md.write_text(md, encoding="utf-8")
        run_md.write_text(md, encoding="utf-8")

        print(f"DWH snapshot written: {latest_md}")
        print(f"DWH snapshot archive: {run_md}")

    result = {
        "status": snapshot["status"],
        "summary": snapshot["summary"],
        "missing_project_files": snapshot["missing_project_files"],
        "forbidden_import_findings": snapshot["forbidden_import_findings"],
        "json_errors": snapshot["json_errors"],
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))

    if snapshot["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
