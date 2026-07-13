from __future__ import annotations

import os
from typing import Any, Dict, List

from dotenv import load_dotenv
from google.cloud import bigquery

from comparison_core import ColumnInfo, ObjectInfo, plain

NUMERIC = {"INT64", "INTEGER", "FLOAT", "FLOAT64", "NUMERIC", "BIGNUMERIC"}
COMPLEX = {"ARRAY", "STRUCT", "RECORD", "GEOGRAPHY", "JSON"}


class BigQueryProvider:
    name = "bigquery"

    def __init__(self, config: Dict[str, Any] | None = None):
        config = config or {}
        load_dotenv(config.get("connection", {}).get("env_file", ".env"))
        self.project_id = config.get("project") or os.getenv(config.get("project_env", "GCP_PROJECT_ID")) or os.getenv("GOOGLE_CLOUD_PROJECT")
        if not self.project_id:
            raise ValueError("Missing project id. Set GCP_PROJECT_ID in .env.")
        self.location = config.get("provider_options", {}).get("location") or os.getenv("BQ_LOCATION")
        self.client = bigquery.Client(project=self.project_id, location=self.location or None)

    def list_namespaces(self) -> List[str]:
        return sorted(d.dataset_id for d in self.client.list_datasets(project=self.project_id))

    def list_objects(self, namespace: str) -> List[Dict[str, Any]]:
        result = []
        for item in self.client.list_tables(f"{self.project_id}.{namespace}"):
            result.append({"namespace": namespace, "name": item.table_id, "object_type": (item.table_type or "TABLE").upper()})
        return sorted(result, key=lambda x: x["name"].lower())

    def search_objects(self, contains: str = "", namespace_contains: str = "") -> List[Dict[str, Any]]:
        out = []
        for namespace in self.list_namespaces():
            if namespace_contains and namespace_contains.lower() not in namespace.lower():
                continue
            try:
                out.extend(x for x in self.list_objects(namespace) if contains.lower() in x["name"].lower())
            except Exception:
                continue
        return out

    def get_object(self, namespace: str, object_name: str) -> ObjectInfo:
        table = self.client.get_table(f"{self.project_id}.{namespace}.{object_name}")
        columns = []
        for field in table.schema:
            data_type = str(field.field_type or "").upper()
            mode = str(field.mode or "NULLABLE").upper()
            columns.append(ColumnInfo(field.name, data_type, mode != "REQUIRED", mode, field.description or "", data_type != "GEOGRAPHY"))
        partition_fields = []
        if getattr(table, "time_partitioning", None) and table.time_partitioning.field:
            partition_fields.append(table.time_partitioning.field)
        return ObjectInfo(self.project_id, namespace, object_name, (table.table_type or "TABLE").upper(), columns, partition_fields)

    @staticmethod
    def qi(value: str) -> str:
        return "`" + str(value).replace("`", "``") + "`"

    def ref(self, obj: ObjectInfo) -> str:
        return f"{self.qi(obj.catalog)}.{self.qi(obj.schema)}.{self.qi(obj.name)}"

    def _where(self, side: str, obj: ObjectInfo, config: Dict[str, Any]) -> str:
        conditions = list(config.get("filters", {}).get(side, []) or [])
        date_filter = config.get("date_filter", {}) or {}
        field_name = date_filter.get(f"{side}_field") or date_filter.get("field")
        if field_name:
            column = next((c for c in obj.columns if c.name.lower() == str(field_name).lower()), None)
            if not column:
                raise ValueError(f"Date field '{field_name}' not found on {side}.")
            if date_filter.get("from"):
                conditions.append(f"DATE({self.qi(field_name)}) >= DATE('{date_filter['from']}')")
            if date_filter.get("to"):
                conditions.append(f"DATE({self.qi(field_name)}) <= DATE('{date_filter['to']}')")
        return " AND ".join(f"({condition})" for condition in conditions)

    def _key_string(self, alias: str, keys: List[str]) -> str:
        parts = []
        for key in keys:
            value = f"{alias}.{self.qi(key)}"
            parts.append(f"IF({value} IS NULL, '<NULL>', CONCAT(LENGTH(CAST({value} AS STRING)), ':', CAST({value} AS STRING)))")
        return "CONCAT(" + ", '|', ".join(parts) + ")"

    def _value(self, alias: str, column: Dict[str, Any], config: Dict[str, Any]) -> str:
        value = f"{alias}.{self.qi(column['name'])}"
        data_type = column["data_type"]
        if data_type == "STRING":
            options = config.get("comparison_options", {}).get("strings", {})
            expression = value
            if options.get("trim", False):
                expression = f"TRIM({expression})"
            if not options.get("case_sensitive", True):
                expression = f"LOWER({expression})"
            if options.get("empty_string_equals_null", False):
                expression = f"NULLIF({expression}, '')"
            return expression
        if data_type in COMPLEX:
            return f"TO_JSON_STRING({value})"
        return value

    def _different(self, column: Dict[str, Any], config: Dict[str, Any]) -> str:
        name = column["name"]
        left_value = f"l.{self.qi(name)}"
        right_value = f"r.{self.qi(name)}"
        if column["data_type"] in NUMERIC:
            tolerance = float(config.get("comparison_options", {}).get("numeric_tolerance", {}).get("value", 0.001))
            return (
                f"(({left_value} IS NULL) != ({right_value} IS NULL) OR "
                f"({left_value} IS NOT NULL AND {right_value} IS NOT NULL AND "
                f"ABS(CAST({left_value} AS BIGNUMERIC) - CAST({right_value} AS BIGNUMERIC)) > CAST({tolerance} AS BIGNUMERIC)))"
            )
        return f"{self._value('l', column, config)} IS DISTINCT FROM {self._value('r', column, config)}"

    def _source(self, side: str, obj: ObjectInfo, config: Dict[str, Any], selected: List[str], keys: List[str]) -> str:
        where_clause = self._where(side, obj, config)
        names = list(dict.fromkeys(keys + selected))
        fields = ", ".join(self.qi(name) for name in names)
        base = f"SELECT {fields} FROM {self.ref(obj)}" + (f" WHERE {where_clause}" if where_clause else "")
        key_expression = self._key_string("s", keys)
        return (
            "SELECT s.*, 1 AS __row_marker,\n"
            f"       {key_expression} AS __business_key,\n"
            f"       TO_HEX(SHA256({key_expression})) AS __surrogate_key\n"
            f"FROM ({base}) AS s"
        )

    def generate_keyed_sql(self, config: Dict[str, Any], left: ObjectInfo, right: ObjectInfo, plan: Dict[str, Any]) -> str:
        keys = plan["join_keys"]
        columns = plan["compared_columns"]
        selected = [column["name"] for column in columns]
        left_source = self._source("left", left, config, selected, keys)
        right_source = self._source("right", right, config, selected, keys)

        difference_definitions = []
        for index, column in enumerate(columns):
            difference_definitions.append((column, f"__diff_{index}", self._different(column, config)))

        difference_select = ",\n       ".join(f"{expression} AS {alias}" for _, alias, expression in difference_definitions)
        any_difference = " OR ".join(alias for _, alias, _ in difference_definitions) or "FALSE"
        total_differences = " + ".join(f"CAST({alias} AS INT64)" for _, alias, _ in difference_definitions) or "0"

        field_count_queries = [
            f"SELECT '{column['name']}' AS field, COUNTIF({alias}) AS difference_count FROM comparison WHERE __left_present AND __right_present"
            for column, alias, _ in difference_definitions
        ]
        field_counts_inner = "\nUNION ALL\n".join(field_count_queries) if field_count_queries else "SELECT '' AS field, 0 AS difference_count"

        sample_arrays = [
            f"IF({alias}, [STRUCT('{column['name']}' AS field, CAST(l.{self.qi(column['name'])} AS STRING) AS left_value, CAST(r.{self.qi(column['name'])} AS STRING) AS right_value)], [])"
            for column, alias, _ in difference_definitions
        ]
        sample_difference_array = "ARRAY_CONCAT(" + ", ".join(sample_arrays) + ")" if sample_arrays else "[]"
        sample_limit = int(config.get("comparison_options", {}).get("sample", {}).get("max_problem_rows", 10))
        null_predicate = " OR ".join(f"{self.qi(key)} IS NULL" for key in keys)

        comparison_extra = f",\n       {difference_select}" if difference_select else ""

        return f"""
WITH
left_data AS (
  {left_source}
),
right_data AS (
  {right_source}
),
left_stats AS (
  SELECT COUNT(*) AS row_count,
         COUNTIF({null_predicate}) AS null_key_count
  FROM left_data
),
right_stats AS (
  SELECT COUNT(*) AS row_count,
         COUNTIF({null_predicate}) AS null_key_count
  FROM right_data
),
left_duplicates AS (
  SELECT COUNT(*) AS duplicate_key_count,
         ARRAY_AGG(STRUCT(__business_key AS business_key, row_count) ORDER BY row_count DESC LIMIT 10) AS samples
  FROM (
    SELECT __business_key, COUNT(*) AS row_count
    FROM left_data
    GROUP BY __business_key
    HAVING COUNT(*) > 1
  )
),
right_duplicates AS (
  SELECT COUNT(*) AS duplicate_key_count,
         ARRAY_AGG(STRUCT(__business_key AS business_key, row_count) ORDER BY row_count DESC LIMIT 10) AS samples
  FROM (
    SELECT __business_key, COUNT(*) AS row_count
    FROM right_data
    GROUP BY __business_key
    HAVING COUNT(*) > 1
  )
),
joined AS (
  SELECT left_data AS l, right_data AS r
  FROM left_data
  FULL OUTER JOIN right_data
    ON left_data.__surrogate_key = right_data.__surrogate_key
   AND left_data.__business_key = right_data.__business_key
),
comparison AS (
  SELECT l,
         r,
         l.__row_marker IS NOT NULL AS __left_present,
         r.__row_marker IS NOT NULL AS __right_present{comparison_extra}
  FROM joined
),
metrics AS (
  SELECT COUNTIF(__left_present AND NOT __right_present) AS missing_in_right_count,
         COUNTIF(NOT __left_present AND __right_present) AS missing_in_left_count,
         COUNTIF(__left_present AND __right_present) AS matched_key_count,
         COUNTIF(__left_present AND __right_present AND ({any_difference})) AS rows_with_differences_count,
         SUM(IF(__left_present AND __right_present, {total_differences}, 0)) AS total_field_differences
  FROM comparison
),
field_counts AS (
  SELECT ARRAY_AGG(STRUCT(field, difference_count) ORDER BY difference_count DESC, field) AS values
  FROM (
    {field_counts_inner}
  )
  WHERE difference_count > 0
),
samples AS (
  SELECT ARRAY(
    SELECT AS STRUCT
      IF(NOT __left_present, 'MISSING_IN_LEFT', IF(NOT __right_present, 'MISSING_IN_RIGHT', 'FIELD_MISMATCH')) AS difference_type,
      COALESCE(l.__business_key, r.__business_key) AS business_key,
      IF(__left_present AND __right_present, {sample_difference_array}, []) AS differences
    FROM comparison
    WHERE NOT __left_present OR NOT __right_present OR ({any_difference})
    LIMIT {sample_limit}
  ) AS values
)
SELECT left_stats.row_count AS left_row_count,
       right_stats.row_count AS right_row_count,
       left_stats.null_key_count AS left_null_key_count,
       right_stats.null_key_count AS right_null_key_count,
       left_duplicates.duplicate_key_count AS left_duplicate_key_count,
       right_duplicates.duplicate_key_count AS right_duplicate_key_count,
       left_duplicates.samples AS left_duplicate_samples,
       right_duplicates.samples AS right_duplicate_samples,
       metrics.*,
       field_counts.values AS field_difference_counts,
       samples.values AS problem_rows_sample
FROM left_stats
CROSS JOIN right_stats
CROSS JOIN left_duplicates
CROSS JOIN right_duplicates
CROSS JOIN metrics
CROSS JOIN field_counts
CROSS JOIN samples
""".strip()

    def compare(self, config: Dict[str, Any], left: ObjectInfo, right: ObjectInfo, plan: Dict[str, Any]) -> Dict[str, Any]:
        if config.get("comparison_mode", "keyed") != "keyed":
            raise NotImplementedError("Only keyed mode is implemented in this version.")
        sql = self.generate_keyed_sql(config, left, right, plan)
        options = config.get("provider_options", {})
        max_bytes = int(float(options.get("maximum_bytes_billed_gb", 25)) * 1024**3)
        estimated = None
        if options.get("dry_run_before_execute", True):
            dry_run = self.client.query(sql, job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False))
            estimated = int(dry_run.total_bytes_processed or 0)
            if options.get("stop_if_dry_run_exceeds_limit", True) and max_bytes and estimated > max_bytes:
                return {"execution_status": "BLOCKED_SAFETY_LIMIT", "estimated_bytes": estimated, "max_bytes_billed": max_bytes, "result": None, "sqls": [{"name": "comparison_sql", "sql": sql}]}
        job_config = bigquery.QueryJobConfig(use_query_cache=False, maximum_bytes_billed=max_bytes or None)
        rows = list(self.client.query(sql, job_config=job_config).result(timeout=int(config.get("execution", {}).get("query_timeout_seconds", 900))))
        result = plain(rows[0]) if rows else {}
        return {"execution_status": "COMPLETED", "estimated_bytes": estimated, "max_bytes_billed": max_bytes, "result": result, "sqls": [{"name": "comparison_sql", "sql": sql}]}
