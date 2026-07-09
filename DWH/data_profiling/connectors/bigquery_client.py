from __future__ import annotations
import os
from typing import List, Optional

class BigQueryRunner:
    def __init__(self, project_id: Optional[str] = None):
        self.project_id = project_id or os.getenv("GCP_PROJECT_ID")
        self._client = None

    @property
    def client(self):
        if self._client is None:
            from google.cloud import bigquery
            self._client = bigquery.Client(project=self.project_id)
        return self._client

    def run_sql(self, sql):
        return self.client.query(sql).to_dataframe()

class BigQueryMetadataFetcher:
    def __init__(self, project_id: Optional[str] = None, dataset_id: Optional[str] = None):
        self.project_id = project_id or os.getenv("GCP_PROJECT_ID")
        self.dataset_id = dataset_id or os.getenv("BQ_DATASET_ID")
        self._client = None

    @property
    def client(self):
        if self._client is None:
            from google.cloud import bigquery
            self._client = bigquery.Client(project=self.project_id)
        return self._client

    def build_schema_context(self, tables: List[str], project_id: Optional[str] = None, dataset_id: Optional[str] = None):
        project = project_id or self.project_id
        dataset = dataset_id or self.dataset_id
        if not project:
            raise ValueError("Missing project_id. Set GCP_PROJECT_ID or bq_metadata.project_id in rules YAML.")
        if not dataset:
            raise ValueError("Missing dataset_id. Set BQ_DATASET_ID or bq_metadata.dataset_id in rules YAML.")
        chunks = []
        for table_name in tables:
            table_ref = f"{project}.{dataset}.{table_name}"
            table = self.client.get_table(table_ref)
            lines = [f"TABLE: `{table_ref}`", f"DESCRIPTION: {table.description or ''}", "COLUMNS:"]
            for field in table.schema:
                lines.append(f"- {field.name} ({field.field_type}, mode={field.mode}): {field.description or ''}")
            if table.time_partitioning:
                lines.append(f"PARTITIONING: {table.time_partitioning.field or table.time_partitioning.type_}")
            if table.clustering_fields:
                lines.append(f"CLUSTERING: {', '.join(table.clustering_fields)}")
            chunks.append("\n".join(lines))
        return "\n\n---\n\n".join(chunks)
