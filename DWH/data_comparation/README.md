# Data Comparison Tool

## Main entry point

Run this for the start menu:

```powershell
python DWH/data_comparation/scripts/bigquery_compare_tables.py
```

Use this to create a config directly:

```powershell
python DWH/data_comparation/scripts/bigquery_compare_tables.py --wizard
```

Run a saved config:

```powershell
python DWH/data_comparation/scripts/bigquery_compare_tables.py `
  --config DWH/data_comparation/config/my_config.yaml `
  --html-report
```

## Report generator

The report script can now also be run without parameters. It opens a small menu and can use the latest JSON:

```powershell
python DWH/data_comparation/scripts/compare_tables_report.py
```

Or with explicit JSON:

```powershell
python DWH/data_comparation/scripts/compare_tables_report.py `
  --input DWH/data_comparation/outputs/comparison_results/comparison_run_YYYYMMDD_HHMMSS.json
```

## Latest changes

- Fixed BigQuery SQL issue caused by aggregate functions inside UNNEST.
- The HTML card title now wraps long comparison names and object names.
- No noisy warning is displayed for full scan configuration.
- Generated SQL is included in the report in a collapsed expandable section.
- Join key nulls and duplicates remain blocking.
- Non-key column schema differences are reported and excluded, not blocking.
