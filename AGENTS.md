# AGENTS.md â€” SCHOTT_DWH_local

## Repository purpose

This repository owns SCHOTT warehouse / DWH support code.

It is intentionally separate from `SCHOTT_DA_local` and `SCHOTT_DF`.

## Owns

- DWH utilities
- BigQuery connector helpers
- SQL utility helpers used by warehouse workflows
- warehouse metadata helpers
- DWH diagnostics and validation tools
- DWH-specific RunHub actions

## Does not own

- Data Agent Stage 1 / Stage 2 / Stage 3 lifecycle logic
- DA configs, rules, benchmarks, promotion workflows
- Dataform transformation project files
- DA RunHub Journey

## Safety rules

- Do not commit credentials or tokens.
- Generated outputs must go under ignored output folders.
- Do not add imports from `DA` or `agent_eval` into DWH code.
- If a utility is required by both DA and DWH, review whether it belongs in a future `SCHOTT_COMMON` package.

## Current migration status

This project shell may initially be copied from `SCHOTT_DA_local/DWH`.
Do not delete `DWH/` from `SCHOTT_DA_local` until DA no longer imports local DWH modules.