#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
from pathlib import Path
from typing import Any, Dict, List

BASE = Path("DWH") / "data_comparation"
RESULT_DIR = BASE / "outputs" / "comparison_results"
REPORT_DIR = BASE / "outputs" / "reports"


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def num(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except Exception:
        return esc(value)


def display(value: Any) -> str:
    return "Not available" if value is None else num(value)


def short_object(value: Any) -> str:
    parts = str(value or "").split(".")
    return ".".join(parts[-2:]) if len(parts) >= 3 else str(value or "")


def badge(status: str) -> str:
    value = (status or "UNKNOWN").upper()
    css = "ok" if value == "PASSED" else "warn" if value == "WARNING" else "bad" if value in {
        "FAILED", "FAILED_QUERY", "BLOCKED_INVALID_KEY", "BLOCKED_SAFETY_LIMIT", "BLOCKED_SCHEMA_OR_KEY"
    } else "neutral"
    return f'<span class="badge {css}">{esc(value)}</span>'


def metric(label: str, value: Any, kind: str = "") -> str:
    css = f"metric {kind}".strip()
    return f'<div class="{css}"><small>{esc(label)}</small><strong>{display(value)}</strong></div>'


def issue_chips(comparison: Dict[str, Any]) -> str:
    chips = []
    for label, key in [
        ("Schema", "schema_status"),
        ("Keys", "key_status"),
        ("Data", "data_status"),
        ("Execution", "execution_status"),
    ]:
        value = (comparison.get(key) or "").upper()
        if value not in {"PASSED", "COMPLETED", "NOT_RUN", "PENDING", ""}:
            chips.append(f'<span class="issue-chip">{esc(label)} {badge(value)}</span>')
    return "".join(chips) or '<span class="muted">No active issues in status checks.</span>'


def schema_html(schema: Dict[str, Any]) -> str:
    rows = []
    for item in schema.get("missing_in_left", []) or []:
        rows.append(f'<tr><td>{esc(item.get("name"))}</td><td>Missing in left</td><td>{esc(item.get("data_type"))}</td></tr>')
    for item in schema.get("missing_in_right", []) or []:
        rows.append(f'<tr><td>{esc(item.get("name"))}</td><td>Missing in right</td><td>{esc(item.get("data_type"))}</td></tr>')
    for item in schema.get("data_type_mismatches", []) or []:
        rows.append(f'<tr><td>{esc(item.get("name"))}</td><td>Type mismatch</td><td>{esc(item.get("left_type"))} vs {esc(item.get("right_type"))}</td></tr>')
    for item in schema.get("mode_mismatches", []) or []:
        rows.append(f'<tr><td>{esc(item.get("name"))}</td><td>Mode mismatch</td><td>{esc(item.get("left_mode"))} vs {esc(item.get("right_mode"))}</td></tr>')
    if not rows:
        return '<p class="muted compact-p">No schema differences.</p>'
    return '<table class="compact-table"><thead><tr><th>Column</th><th>Issue</th><th>Detail</th></tr></thead><tbody>' + "".join(rows) + "</tbody></table>"


def key_quality(comparison: Dict[str, Any]) -> str:
    key = comparison.get("key_validation") or {}
    values = [
        ("Left null keys", key.get("left_null_key_count")),
        ("Right null keys", key.get("right_null_key_count")),
        ("Left duplicate keys", key.get("left_duplicate_key_count")),
        ("Right duplicate keys", key.get("right_duplicate_key_count")),
    ]
    has_problem = any(int(value or 0) for _, value in values)
    if not has_problem and all(value is not None for _, value in values):
        return '<p class="ok-line">Join keys are clean: no nulls and no duplicates.</p>'
    return '<div class="metrics compact">' + "".join(
        metric(label, value, "problem" if int(value or 0) else "") for label, value in values
    ) + "</div>"


def compared_columns_html(schema: Dict[str, Any], data_result: Dict[str, Any]) -> str:
    compared = schema.get("compared_columns") or []
    names = []
    for item in compared:
        if isinstance(item, dict):
            name = item.get("name")
        else:
            name = str(item)
        if name:
            names.append(str(name))

    counts = {
        str(item.get("field")): int(item.get("difference_count") or 0)
        for item in (data_result.get("field_difference_counts") or [])
        if item.get("field")
    }
    different = [(name, counts.get(name, 0)) for name in names if counts.get(name, 0) > 0]
    identical = [name for name in names if counts.get(name, 0) == 0]

    summary = (
        f'<div class="column-summary">'
        f'<span><strong>{len(names)}</strong> compared</span>'
        f'<span class="ok-text"><strong>{len(identical)}</strong> identical</span>'
        f'<span class="bad-text"><strong>{len(different)}</strong> different</span>'
        f'</div>'
    )

    if different:
        failing = "".join(
            f'<div class="bar problem-bar"><span>{esc(name)}</span><b>{num(count)} differences</b></div>'
            for name, count in sorted(different, key=lambda x: (-x[1], x[0].lower()))
        )
    else:
        failing = '<p class="ok-line">All compared columns are identical.</p>'

    all_rows = "".join(
        f'<tr><td>{esc(name)}</td><td>{badge("FAILED") if counts.get(name, 0) else badge("PASSED")}</td><td>{num(counts.get(name, 0))}</td></tr>'
        for name in names
    )
    all_columns = (
        '<details class="all-columns"><summary>Show all compared columns</summary>'
        '<table class="compact-table"><thead><tr><th>Field</th><th>Status</th><th>Differences</th></tr></thead>'
        f'<tbody>{all_rows}</tbody></table></details>'
    ) if names else '<p class="muted compact-p">No comparable columns.</p>'

    return summary + failing + all_columns


def sample_table(rows: List[Dict[str, Any]]) -> str:
    flat = []
    for index, row in enumerate(rows or [], 1):
        differences = row.get("differences") or []
        if differences:
            for difference in differences:
                flat.append((index, row.get("business_key"), difference.get("field"), difference.get("left_value"), difference.get("right_value")))
        else:
            flat.append((index, row.get("business_key"), row.get("difference_type"), "", ""))
    if not flat:
        return '<p class="muted compact-p">No problematic samples.</p>'
    body = "".join(
        f'<tr><td>{index}</td><td><code>{esc(key)}</code></td><td>{esc(field)}</td><td class="left-val"><code>{esc(left)}</code></td><td class="right-val"><code>{esc(right)}</code></td></tr>'
        for index, key, field, left, right in flat
    )
    return '<table class="compact-table samples"><thead><tr><th>#</th><th>Business key</th><th>Field</th><th>Left</th><th>Right</th></tr></thead><tbody>' + body + "</tbody></table>"


def sql_html(sqls: List[Dict[str, Any]]) -> str:
    if not sqls:
        return '<p class="muted compact-p">SQL was not generated.</p>'
    return "".join(
        f'<details class="sql"><summary>{esc(item.get("name", "SQL"))}</summary><pre>{esc(item.get("sql"))}</pre></details>'
        for item in sqls
    )


def comparison_card(comparison: Dict[str, Any]) -> str:
    data = comparison.get("data_result") or {}
    schema = comparison.get("schema") or {}
    left = short_object(comparison.get("left_object"))
    right = short_object(comparison.get("right_object"))
    errors = "".join(f'<li>{esc(item)}</li>' for item in comparison.get("errors", []) or [])

    problem_metrics = "".join([
        metric("Missing in right", data.get("missing_in_right_count"), "problem" if int(data.get("missing_in_right_count") or 0) else ""),
        metric("Missing in left", data.get("missing_in_left_count"), "problem" if int(data.get("missing_in_left_count") or 0) else ""),
        metric("Changed rows", data.get("rows_with_differences_count"), "problem" if int(data.get("rows_with_differences_count") or 0) else ""),
        metric("Total differences", data.get("total_differences"), "problem" if int(data.get("total_differences") or 0) else ""),
    ])
    context_metrics = metric("Left rows", data.get("left_row_count")) + metric("Right rows", data.get("right_row_count"))

    return f'''
<section class="card">
  <header>
    <div class="titlebox">
      <h2 title="{esc(comparison.get('comparison_name'))}">{esc(comparison.get('comparison_name'))}</h2>
      <div class="objects">
        <div><span class="side left">Left</span><code>{esc(left)}</code></div>
        <div><span class="side right">Right</span><code>{esc(right)}</code></div>
        <div><strong>Join keys:</strong> <code>{esc(', '.join(comparison.get('join_keys') or []))}</code></div>
      </div>
    </div>
    {badge(comparison.get('overall_status'))}
  </header>
  <div class="issues">{issue_chips(comparison)}</div>
  <div class="metrics context">{context_metrics}</div>
  <div class="metrics problems">{problem_metrics}</div>
  {f'<div class="alert"><strong>Errors</strong><ul>{errors}</ul></div>' if errors else ''}
  <div class="grid2">
    <section><h3>Schema issues</h3><p class="muted compact-p">Compared {len(schema.get('compared_columns') or [])} · Excluded {len(schema.get('excluded_columns') or [])}</p>{schema_html(schema)}</section>
    <section><h3>Join key quality</h3>{key_quality(comparison)}</section>
  </div>
  <h3>Compared columns</h3>{compared_columns_html(schema, data)}
  <h3>Problem samples</h3>{sample_table(data.get('problem_rows_sample') or [])}
  <h3>Generated SQL</h3>{sql_html(comparison.get('sqls') or [])}
</section>'''


def generate_report(input_path: Path, output_dir: Path = REPORT_DIR) -> Path:
    data = json.loads(Path(input_path).read_text(encoding="utf-8"))
    summary = data.get("summary", {})
    output_dir.mkdir(parents=True, exist_ok=True)
    cards = "".join(comparison_card(item) for item in data.get("comparisons", []) or [])

    document = f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Data Comparison Report</title><style>
:root{{--bg:#f4f7fb;--ink:#152238;--muted:#667085;--line:#e3e8ef;--ok:#067647;--bad:#b42318;--warn:#b54708;--left:#175cd3;--right:#c4320a}}*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:14px Segoe UI,Arial,sans-serif}}.hero{{background:linear-gradient(125deg,#10243e,#2459c4);color:white;padding:24px 5vw}}.hero h1{{margin:0 0 5px;font-size:28px}}main{{max-width:1180px;margin:auto;padding:16px}}.summary,.metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(135px,1fr));gap:8px}}.metric,.card{{background:white;border:1px solid var(--line);border-radius:14px}}.metric{{padding:11px}}.metric small{{display:block;color:var(--muted);margin-bottom:4px}}.metric strong{{font-size:20px}}.metric.problem{{border-color:#fda29b;background:#fff7f6}}.card{{padding:16px;margin-top:14px;box-shadow:0 8px 24px #10243e0d;overflow:hidden}}.card header{{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}}.titlebox{{min-width:0;max-width:calc(100% - 85px)}}h2{{margin:0 0 8px;font-size:21px;line-height:1.18;overflow-wrap:anywhere;word-break:break-word}}h3{{font-size:15px;border-top:1px solid var(--line);padding-top:12px;margin:14px 0 8px}}.muted{{color:var(--muted)}}.compact-p{{margin:4px 0 8px}}.badge{{display:inline-block;padding:5px 9px;border-radius:999px;font-size:11px;font-weight:700;white-space:nowrap}}.ok{{background:#dcfae6;color:var(--ok)}}.bad{{background:#fee4e2;color:var(--bad)}}.warn{{background:#fef0c7;color:var(--warn)}}.neutral{{background:#eef2f6;color:#475467}}.objects{{display:grid;gap:5px;margin-bottom:8px}}.side{{display:inline-block;width:46px;padding:2px 6px;border-radius:999px;color:white;font-weight:700;font-size:12px;text-align:center;margin-right:6px}}.side.left{{background:var(--left)}}.side.right{{background:var(--right)}}code{{background:#f1f4f8;padding:2px 5px;border-radius:5px;white-space:normal;overflow-wrap:anywhere}}.left-val code{{border-left:4px solid var(--left)}}.right-val code{{border-left:4px solid var(--right)}}.issues{{margin:8px 0;display:flex;gap:8px;flex-wrap:wrap}}.issue-chip{{background:#fff;border:1px solid var(--line);border-radius:999px;padding:4px 8px}}.context .metric strong{{font-size:18px;color:#475467}}.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}table{{width:100%;border-collapse:collapse}}th,td{{padding:7px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}th{{color:var(--muted);font-size:12px}}.samples td:nth-child(2){{max-width:440px;overflow-wrap:anywhere}}details{{border:1px solid var(--line);border-radius:10px;padding:9px;margin:7px 0}}summary{{cursor:pointer;font-weight:600}}.alert{{background:#fff1f0;color:#7a271a;padding:10px;border-radius:10px;margin:10px 0}}.bar{{display:flex;justify-content:space-between;padding:6px 9px;background:#f7f9fc;border:1px solid var(--line);border-radius:8px;margin:4px 0}}.problem-bar{{background:#fff7f6;border-color:#fecdca}}.ok-line{{background:#ecfdf3;color:#067647;border-radius:9px;padding:9px;margin:0}}.column-summary{{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:8px}}.column-summary span{{background:#f7f9fc;border:1px solid var(--line);padding:6px 9px;border-radius:9px}}.ok-text{{color:var(--ok)}}.bad-text{{color:var(--bad)}}.all-columns{{margin-top:9px}}pre{{white-space:pre-wrap;overflow:auto;background:#0b1220;color:#d9e5ff;border-radius:10px;padding:12px;max-height:420px}}@media(max-width:760px){{main{{padding:10px}}.card header{{display:block}}.titlebox{{max-width:100%}}.grid2{{grid-template-columns:1fr}}}}
</style></head><body><div class="hero"><h1>Data Comparison Report {badge(summary.get('status'))}</h1><div>Run {esc(data.get('run_id'))} · {esc(data.get('generated_at'))}</div></div><main><section class="summary">{metric('Comparisons', summary.get('total'))}{metric('Passed', summary.get('passed'))}{metric('Warnings', summary.get('warnings'))}{metric('Failed', summary.get('failed'), 'problem' if int(summary.get('failed') or 0) else '')}</section>{cards}</main></body></html>'''

    output_path = output_dir / f"comparison_run_{data.get('run_id')}.html"
    output_path.write_text(document, encoding="utf-8")
    return output_path


def interactive() -> Path:
    files = sorted(RESULT_DIR.glob("comparison_run_*.json"), reverse=True)
    print("\nComparison report generator")
    print("===========================")
    if files:
        print("[1] Generate report from latest JSON")
        print("[2] Choose JSON from output folder")
        print("[3] Enter JSON path manually")
        option = input("Selection [1]: ").strip() or "1"
        if option == "1":
            return generate_report(files[0], REPORT_DIR)
        if option == "2":
            for index, path in enumerate(files, 1):
                print(f"[{index}] {path.name}")
            return generate_report(files[int(input("Selection: ")) - 1], REPORT_DIR)
    return generate_report(Path(input("JSON path: ").strip()), REPORT_DIR)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input")
    parser.add_argument("--output-dir", default=str(REPORT_DIR))
    args = parser.parse_args()
    print(generate_report(Path(args.input), Path(args.output_dir)) if args.input else interactive())
