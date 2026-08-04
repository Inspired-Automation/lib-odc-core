from unittest.mock import MagicMock, patch

from odc_core import duplicate_check

TABLES = {
    "jobs": "ODC_jobs",
    "job_details": "ODC_job_details",
    "scrape_data": "ODC_scrape_data",
    "scrape_accounts": "ODC_scrape_accounts",
    "web_scrape_data": "web_scrape_data",
}


def _connect_mock(row):
    cursor = MagicMock()
    cursor.execute.return_value.fetchone.return_value = row
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value = cursor
    return conn, cursor


@patch("odc_core.db.pyodbc.connect")
def test_is_duplicate_standard_client_true(mock_connect):
    conn, cursor = _connect_mock((1,))
    mock_connect.return_value = conn

    result = duplicate_check.is_duplicate("ACC1", "Crown", "Crown Gas", "REF1", TABLES, "Jupiter")

    assert result is True
    sql = cursor.execute.call_args.args[0]
    assert "ODC_scrape_data" in sql
    assert "ODC_scrape_accounts" not in sql


@patch("odc_core.db.pyodbc.connect")
def test_is_duplicate_standard_client_false(mock_connect):
    conn, _ = _connect_mock((0,))
    mock_connect.return_value = conn

    result = duplicate_check.is_duplicate("ACC1", "Crown", "Crown Gas", "REF1", TABLES, "Jupiter")

    assert result is False


@patch("odc_core.db.pyodbc.connect")
def test_is_duplicate_inspired_plc_uses_union_query(mock_connect):
    conn, cursor = _connect_mock((1,))
    mock_connect.return_value = conn

    result = duplicate_check.is_duplicate(
        "ACC1", "Crown", "Inspired PLC", "REF1", TABLES, "Jupiter"
    )

    assert result is True
    sql = cursor.execute.call_args.args[0]
    assert "UNION ALL" in sql
    assert "ODC_scrape_accounts" in sql


@patch("odc_core.db.pyodbc.connect")
def test_is_duplicate_no_row_returns_false(mock_connect):
    conn, _ = _connect_mock(None)
    mock_connect.return_value = conn

    result = duplicate_check.is_duplicate("ACC1", "Crown", "Crown Gas", "REF1", TABLES, "Jupiter")

    assert result is False
