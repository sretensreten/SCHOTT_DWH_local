#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, html
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

DEFAULT_PROJECT_CONFIG_PATH = Path("DWH") / "data_profiling" / "config" / "profiler_project.json"

def get_cfg(config: Dict[str, Any], path: str, default: Any = None) -> Any:
    cur = config
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur

def esc(v: Any) -> str: return html.escape("" if v is None else str(v), quote=True)
def fmt_int(v: Any) -> str:
    try: return f"{int(float(v)):,}"
    except Exception: return "" if v is None else esc(v)
def fmt_dec(v: Any, digits: int = 2) -> str:
    try:
        s = f"{float(v):,.{digits}f}"
        return s.rstrip('0').rstrip('.') if '.' in s else s
    except Exception: return "" if v is None else esc(v)
def dash(v: Any) -> str: return "—" if v is None or str(v) == "" else esc(v)
def numeric_kind(t: str) -> str:
    v = (t or "").lower().strip()
    if v.startswith("decimal") or v in {"double", "float", "real"}: return "decimal"
    if v in {"tinyint", "smallint", "integer", "int", "bigint"}: return "integer"
    return ""
def is_timestamp(t: str) -> bool: return (t or "").lower().strip().startswith("timestamp")
def fmt_minmax(col: Dict[str, Any], key: str) -> str:
    t = col.get("type", ""); st = col.get("stats") or {}
    if key == "min_value" and st.get("min_length") not in {None, ""}: return fmt_int(st.get("min_length"))
    if key == "max_value" and st.get("max_length") not in {None, ""}: return fmt_int(st.get("max_length"))
    val = st.get(key); nk = numeric_kind(t)
    if nk == "integer": return fmt_int(val)
    if nk == "decimal": return fmt_dec(val, 2)
    cls = " class='small-ts'" if is_timestamp(t) else ""
    return f"<span{cls}>{dash(val)}</span>"
def load_config(path: Path) -> Dict[str, Any]: return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

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

def load_profiles(input_dir: Path) -> List[Dict[str, Any]]:
    out=[]
    for p in sorted(input_dir.glob("*.profile.json")):
        try:
            d=json.loads(p.read_text(encoding="utf-8")); d["__source_file"]=p.name; out.append(d)
        except Exception as exc:
            out.append({"name":p.name,"database":"","object_type":"INVALID","columns":[],"failures":[{"scope":"load","error":str(exc)}],"__source_file":p.name})
    return out
def status(p: Dict[str, Any]) -> str:
    if p.get("failures"): return "HAS FAILURES"
    try:
        if int(p.get("sample_limit") or 0)>0 and int(p.get("profiled_rows") or 0)>=int(p.get("sample_limit") or 0): return "SAMPLED"
    except Exception: pass
    return "FULL PROFILE"
def badge_class(s: str) -> str: return "danger" if s=="HAS FAILURES" else "warning" if s=="SAMPLED" else "success"
def display_distinct(col: Dict[str, Any]) -> str:
    if col.get("profile_kind") == "numeric": return "—"
    return fmt_int((col.get("stats") or {}).get("distinct_count")) or "—"
def top_values_html(values: List[Dict[str, Any]], limit: int) -> str:
    if not values: return "<span class='muted'>—</span>"
    max_count=max([int(v.get("count") or 0) for v in values[:limit]]+[1]); parts=[]
    for item in values[:limit]:
        count=int(item.get("count") or 0); width=max(3,int((count/max_count)*100))
        parts.append("<div class='tv'>"+f"<span title='{esc(item.get('value'))}'>{esc(item.get('value'))}</span>"+f"<b><i style='width:{width}%'></i></b><em>{fmt_int(count)}</em></div>")
    return "<details class='top-details'><summary>Show top values</summary><div class='top-body'>"+"".join(parts)+"</div></details>"
def object_html(p: Dict[str, Any], top_limit: int) -> str:
    s=status(p); col_rows=[]
    for c in p.get("columns") or []:
        st=c.get("stats") or {}
        col_rows.append(f"""<tr><td class='col-name'><strong>{esc(c.get('name'))}</strong><small>{esc(c.get('comment'))}</small></td><td><code>{esc(c.get('type'))}</code></td><td>{fmt_int(st.get('null_count'))}</td><td>{display_distinct(c)}</td><td>{fmt_minmax(c,'min_value')}</td><td>{fmt_minmax(c,'max_value')}</td><td>{top_values_html(st.get('top_values') or [], top_limit)}</td></tr>""")
    failures=""
    if p.get("failures"):
        items="".join([f"<li><strong>{esc(f.get('scope'))}</strong>: {esc(f.get('error'))}</li>" for f in p.get("failures")]); failures=f"<div class='fail'><strong>Failures</strong><ul>{items}</ul></div>"
    profiled_time = p.get("profiled_at") or ""
    return f"""<details class='object-card' data-status='{esc(s)}' data-type='{esc(p.get('object_type'))}'><summary class='object-summary'><div class='object-main'><h2>{esc(p.get('name'))}</h2><p>{esc(p.get('database'))} · {esc(p.get('__source_file'))}</p></div><div class='profile-time'>{esc(profiled_time)}</div><div class='badges'><span class='badge type'>{esc(p.get('object_type'))}</span><span class='badge {badge_class(s)}'>{s}</span></div></summary><div class='object-body'><div class='mini-grid'><div><span>Profiled rows</span><strong>{fmt_int(p.get('profiled_rows'))}</strong></div><div><span>Sample limit</span><strong>{fmt_int(p.get('sample_limit'))}</strong></div><div><span>Columns</span><strong>{fmt_int(len(p.get('columns') or []))}</strong></div><div><span>Runtime</span><strong>{fmt_dec(p.get('elapsed_seconds'),2)}s</strong></div></div>{failures}<div class='table-wrap'><table><thead><tr><th>Column</th><th>Type</th><th>Nulls</th><th>Distinct</th><th>Min</th><th>Max</th><th>Top values</th></tr></thead><tbody>{''.join(col_rows)}</tbody></table></div></div></details>"""
def build_html(profiles: List[Dict[str, Any]], config: Dict[str, Any]) -> str:
    title=get_cfg(config,"reports.title","Data Profile Report"); company=get_cfg(config,"reports.company_label","DWH Data Profiling"); top_limit=int(get_cfg(config,"reports.show_top_values_limit",20)); generated=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    objects=len(profiles); tables=sum(1 for p in profiles if p.get("object_type")=="TABLE"); views=sum(1 for p in profiles if p.get("object_type")=="VIEW"); failures=sum(1 for p in profiles if p.get("failures")); sampled=sum(1 for p in profiles if status(p)=="SAMPLED"); total_rows=sum(int(p.get("profiled_rows") or 0) for p in profiles if str(p.get("profiled_rows") or "").replace('.', '', 1).isdigit()); databases=", ".join(sorted({p.get("database","") for p in profiles if p.get("database")})) or "—"; objects_html="".join(object_html(p, top_limit) for p in profiles)
    return f"""<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>{esc(title)}</title><style>:root{{--bg:#f5f7fa;--card:#fff;--text:#172033;--muted:#667085;--line:#d9e2ec;--brand:#005f73;--brand2:#0a9396;--success:#157347;--warning:#a15c07;--danger:#b42318}}*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);font-family:Segoe UI,Arial,sans-serif;color:var(--text)}}.hero{{background:linear-gradient(135deg,#003b49,#007c89 65%,#0a9396);color:white;padding:34px 42px 78px}}.hero h1{{margin:0 0 8px;font-size:32px}}.hero p{{margin:0;opacity:.88}}.container{{max-width:1460px;margin:-48px auto 60px;padding:0 34px}}.summary{{display:grid;grid-template-columns:repeat(6,1fr);gap:14px;margin-bottom:22px}}.card{{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:18px;box-shadow:0 10px 22px rgba(15,23,42,.08)}}.card span,.mini-grid span{{display:block;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.06em}}.card strong{{display:block;font-size:27px;margin-top:8px}}.toolbar{{display:flex;gap:10px;margin:18px 0;flex-wrap:wrap}}input,select{{border:1px solid var(--line);border-radius:12px;padding:12px 14px;background:white}}input{{flex:1;min-width:280px}}.object-card{{background:white;border:1px solid var(--line);border-radius:18px;margin:16px 0;box-shadow:0 6px 16px rgba(15,23,42,.06);overflow:hidden}}.object-summary{{cursor:pointer;display:grid;grid-template-columns:minmax(0,1fr) 235px 235px;align-items:center;gap:18px;padding:19px 22px;list-style:none}}.object-summary::-webkit-details-marker{{display:none}}.object-main h2{{margin:0;font-size:21px}}.object-main p{{margin:4px 0 0;color:var(--muted);font-size:13px}}.profile-time{{font-size:13px;font-weight:400;color:var(--text);white-space:nowrap;text-align:left;font-variant-numeric:tabular-nums}}.object-body{{padding:0 22px 24px}}.badges{{display:grid;grid-template-columns:96px 124px;gap:8px;justify-content:start;align-items:center}}.badge{{display:inline-flex;justify-content:center;align-items:center;border-radius:999px;padding:5px 9px;font-size:12px;font-weight:700;white-space:nowrap}}.type{{background:#e6f4f7;color:var(--brand)}}.success{{background:#e8f6ef;color:var(--success)}}.warning{{background:#fff4df;color:var(--warning)}}.danger{{background:#fdeaea;color:var(--danger)}}.mini-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:18px}}.mini-grid div{{border:1px solid var(--line);background:#fbfdff;border-radius:14px;padding:14px}}.mini-grid strong{{font-size:20px;display:block;margin-top:6px}}.table-wrap{{overflow:auto;border:1px solid var(--line);border-radius:14px}}table{{border-collapse:collapse;width:100%;min-width:1020px}}th{{background:#eff5f8;text-align:left;font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:#344054;padding:11px;position:sticky;top:0}}td{{padding:11px;border-top:1px solid #edf1f5;vertical-align:top;font-size:13px;text-align:left}}td small{{display:block;color:var(--muted)}}code{{background:#eef3f8;border-radius:7px;padding:3px 6px}}.muted{{color:var(--muted)}}.small-ts{{font-size:70%;line-height:1.2}}.col-name{{width:30ch;max-width:30ch;min-width:30ch;white-space:normal;overflow-wrap:anywhere;word-break:break-word;line-height:1.25}}.top-details summary{{cursor:pointer;color:var(--brand);font-weight:700;padding:0;list-style:none;text-align:left}}.top-details summary::-webkit-details-marker{{display:none}}.top-details summary:before{{content:'▸ ';}}.top-details[open] summary:before{{content:'▾ ';}}.top-body{{margin-top:8px;min-width:330px}}.tv{{display:grid;grid-template-columns:minmax(120px,1.15fr) minmax(100px,2fr) 60px;gap:8px;align-items:center;margin:4px 0}}.tv span{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}.tv b{{height:8px;background:#edf2f7;border-radius:999px;overflow:hidden}}.tv i{{display:block;height:8px;background:linear-gradient(90deg,var(--brand2),var(--brand));border-radius:999px}}.tv em{{font-style:normal;text-align:left;color:var(--muted);font-variant-numeric:tabular-nums}}.fail{{background:#fdeaea;color:#b42318;border:1px solid #f6b4af;border-radius:14px;padding:12px;margin-bottom:14px}}.footer{{text-align:center;color:var(--muted);font-size:12px;margin-top:30px}}@media(max-width:1100px){{.summary{{grid-template-columns:repeat(2,1fr)}}.mini-grid{{grid-template-columns:repeat(2,1fr)}}.object-summary{{grid-template-columns:1fr;align-items:flex-start}}.badges{{grid-template-columns:96px 124px}}}}@media(max-width:720px){{.hero{{padding:26px 20px 60px}}.container{{padding:0 18px}}.summary{{grid-template-columns:1fr}}}}</style></head><body><header class='hero'><h1>{esc(title)}</h1><p>{esc(company)} · Generated {esc(generated)} · Database(s): {esc(databases)}</p></header><main class='container'><section class='summary'><div class='card'><span>Objects</span><strong>{fmt_int(objects)}</strong></div><div class='card'><span>Tables</span><strong>{fmt_int(tables)}</strong></div><div class='card'><span>Views</span><strong>{fmt_int(views)}</strong></div><div class='card'><span>Profiled rows</span><strong>{fmt_int(total_rows)}</strong></div><div class='card'><span>Sampled</span><strong>{fmt_int(sampled)}</strong></div><div class='card'><span>Failures</span><strong>{fmt_int(failures)}</strong></div></section><div class='toolbar'><input id='q' placeholder='Search object, column, value...' oninput='filterReport()'><select id='st' onchange='filterReport()'><option value=''>All statuses</option><option>FULL PROFILE</option><option>SAMPLED</option><option>HAS FAILURES</option></select><select id='tp' onchange='filterReport()'><option value=''>All types</option><option>TABLE</option><option>VIEW</option></select></div>{objects_html}<div class='footer'>Generated from data profile JSON files. Static self-contained HTML.</div></main><script>function filterReport(){{const q=document.getElementById('q').value.toLowerCase();const st=document.getElementById('st').value;const tp=document.getElementById('tp').value;document.querySelectorAll('.object-card').forEach(c=>{{const okQ=!q||c.innerText.toLowerCase().includes(q);const okS=!st||c.dataset.status===st;const okT=!tp||c.dataset.type===tp;c.style.display=(okQ&&okS&&okT)?'block':'none';}})}}</script></body></html>"""
def main() -> int:
    ap=argparse.ArgumentParser(description="Build professional static HTML report from profile JSON files.")
    ap.add_argument("--project-config", default=str(DEFAULT_PROJECT_CONFIG_PATH))
    ap.add_argument("--config", default=None)
    ap.add_argument("--input-dir", default=None)
    ap.add_argument("--output", default=None)
    args=ap.parse_args()
    config_path = Path(args.config) if args.config else resolve_profiler_config_path(Path(args.project_config))
    config=load_config(config_path)
    input_dir_value = args.input_dir or get_cfg(config,"reports.input_dir")
    if not input_dir_value:
        print("[ERROR] Missing reports.input_dir in profiler config. Provide --input-dir or set reports.input_dir.")
        return 1
    input_dir=Path(input_dir_value)
    output=Path(args.output) if args.output else Path(get_cfg(config,"reports.output_dir","DWH/data_profiling/outputs/reports"))/get_cfg(config,"reports.output_file","data_profile_report.html")
    if not input_dir.is_dir(): print(f"[ERROR] Input folder not found: {input_dir}"); return 1
    profiles=load_profiles(input_dir)
    if not profiles: print(f"[ERROR] No *.profile.json files found in: {input_dir}"); return 1
    output.parent.mkdir(parents=True,exist_ok=True); output.write_text(build_html(profiles,config),encoding="utf-8"); print("[OK] HTML report created"); print(f"     Config file    : {config_path}"); print(f"     Input profiles : {len(profiles)}"); print(f"     Output file    : {output}"); return 0
if __name__ == "__main__": raise SystemExit(main())
