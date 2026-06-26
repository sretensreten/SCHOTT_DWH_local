# SCHOTT DWH RunHub engine package

This package is intended to be extracted into:

```text
C:\Users\smiliv01\Documents\SCHOTT_DWH_local
```

It adds only:

```text
.runhub/
```

It does not modify:

```text
DWH/
.runhub.project/
AGENTS.md
README.md
package.json
.vscode/
```

## Start

```powershell
cd C:\Users\smiliv01\Documents\SCHOTT_DWH_local
npm run runhub
```

Open:

```text
http://localhost:3002
```

## First action to test

Run:

```text
Compile DWH Python Files
```

This runs:

```powershell
python -m py_compile DWH/connectors/bigquery_client.py DWH/utils/sql_utils.py
```
