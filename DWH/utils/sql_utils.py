import re

BACKTICK_TABLE_RE = re.compile(r"`([^`]+)`")
FROM_JOIN_RE = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z_][\w.-]*)", re.IGNORECASE)
CTE_RE = re.compile(r"(?:WITH|,)\s*([A-Za-z_][\w]*)\s+AS\s*\(", re.IGNORECASE)

SQL_KEYWORDS = {
    "select", "where", "group", "order", "limit", "on", "using", "left", "right", "inner", "outer",
    "full", "cross", "join", "from", "as", "and", "or", "by", "having", "qualify", "union",
}


def normalize_sql(sql):
    sql = re.sub(r"```sql|```", "", sql or "", flags=re.IGNORECASE).strip()
    sql = re.sub(r"^sql\s*", "", sql, flags=re.IGNORECASE).strip()
    sql = re.sub(r"\s+", " ", sql)
    return sql.rstrip(";").strip()


def substitute_parameters(sql, params):
    out = sql or ""
    for k, v in (params or {}).items():
        out = out.replace(k, str(v))
    return out


def strip_string_literals(sql):
    # Handles common single/double quoted strings. Good enough for validation checks.
    sql = re.sub(r"'([^'\\]|\\.)*'", "''", sql or "")
    sql = re.sub(r'"([^"\\]|\\.)*"', '""', sql)
    return sql


def strip_sql_comments(sql):
    """Remove SQL comments before pattern checks.

    This prevents false positives such as:
    - '/' inside /* block comments */ being detected as direct division.
    - '-- inline comments' hiding following SQL when validators flatten SQL to one line.

    This utility is intentionally lightweight and runs after string literals are stripped
    in contains_direct_division(), so comment markers inside quoted text are already safe.
    """
    sql = sql or ""
    # Remove block comments, including multiline comments.
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    # Remove line comments.
    sql = re.sub(r"--.*?$", "", sql, flags=re.MULTILINE)
    # Remove hash comments as an extra guard.
    sql = re.sub(r"#.*?$", "", sql, flags=re.MULTILINE)
    return sql


def strip_extract_from(sql):
    # Avoid treating EXTRACT(ISOWEEK FROM event_date) as FROM table.
    return re.sub(
        r"EXTRACT\s*\([^)]*?\bFROM\b\s+[A-Za-z_][\w.]*\s*\)",
        "EXTRACT_EXPR",
        sql or "",
        flags=re.IGNORECASE,
    )


def extract_cte_names(sql):
    return sorted(set(m.group(1) for m in CTE_RE.finditer(sql or "")))


def extract_physical_tables(sql):
    sql = sql or ""
    ctes = {c.lower() for c in extract_cte_names(sql)}
    tables = []

    for table in BACKTICK_TABLE_RE.findall(sql):
        short = table.split(".")[-1].strip("`").lower()
        if short not in ctes:
            tables.append(table.strip("`"))

    scrubbed = strip_extract_from(sql)
    for table in FROM_JOIN_RE.findall(scrubbed):
        short = table.split(".")[-1].strip("`").lower()
        if short not in ctes and short not in SQL_KEYWORDS:
            tables.append(table.strip("`"))

    return sorted(set(tables))


# Backward-compatible name.
def extract_tables(sql):
    return extract_physical_tables(sql)


def contains_direct_division(sql):
    cleaned = strip_string_literals(sql or "")
    cleaned = strip_sql_comments(cleaned)
    cleaned = re.sub(r"SAFE_DIVIDE\s*\([^)]*\)", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"https?://\S+", "", cleaned)
    return "/" in cleaned


def has_cross_grain_join(sql):
    s = (sql or "").lower()
    return (
        "pro_pilotpharma_ga4deepdive" in s
        and "pro_pilotpharma_martpageperformancedaily" in s
        and " join " in s
    )


def is_refusal_text(text):
    t = (text or "").lower()
    return any(
        x in t
        for x in [
            "information not available",
            "not integrated",
            "cannot",
            "not tracked",
            "refuse",
            "outside scope",
            "financial data not integrated",
        ]
    )
