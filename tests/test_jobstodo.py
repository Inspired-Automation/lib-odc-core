from unittest.mock import MagicMock, patch

from odc_core import jobstodo

TABLES = {
    "jobs": "ODC_jobs",
    "job_details": "ODC_job_details",
    "scrape_accounts": "ODC_scrape_accounts",
    "suppliers": "ODC_suppliers",
    "multi_credential": "ODC_multi_credentials",
    "credential": "ODC_credentials",
}


def _connect_mock(cursor):
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value = cursor
    return conn


def test_is_multi_credential_job_true():
    assert jobstodo.is_multi_credential_job("multi_credential", "multi_credential") is True


def test_is_multi_credential_job_false():
    assert jobstodo.is_multi_credential_job("jdoe", "secret") is False


def test_is_multi_credential_job_none_values():
    assert jobstodo.is_multi_credential_job(None, None) is False


@patch("odc_core.db.pyodbc.connect")
def test_is_job_claimed_true(mock_connect):
    cursor = MagicMock()
    cursor.fetchone.return_value = ("PROC1",)
    mock_connect.return_value = _connect_mock(cursor)

    assert jobstodo.is_job_claimed("JOB1", TABLES, "Jupiter") is True


@patch("odc_core.db.pyodbc.connect")
def test_is_job_claimed_false_when_no_row(mock_connect):
    cursor = MagicMock()
    cursor.fetchone.return_value = None
    mock_connect.return_value = _connect_mock(cursor)

    assert jobstodo.is_job_claimed("JOB1", TABLES, "Jupiter") is False


@patch("odc_core.db.pyodbc.connect")
def test_get_client_location_returns_value(mock_connect):
    cursor = MagicMock()
    cursor.fetchone.return_value = ("SiteA",)
    mock_connect.return_value = _connect_mock(cursor)

    assert jobstodo.get_client_location("JOB1", TABLES, "Jupiter") == "SiteA"


@patch("odc_core.db.pyodbc.connect")
def test_get_client_location_returns_none_when_missing(mock_connect):
    cursor = MagicMock()
    cursor.fetchone.return_value = None
    mock_connect.return_value = _connect_mock(cursor)

    assert jobstodo.get_client_location("JOB1", TABLES, "Jupiter") is None


@patch("odc_core.db.pyodbc.connect")
def test_get_job_details_single_credential_pending_only(mock_connect):
    cursor = MagicMock()
    cursor.fetchone.return_value = ("jdoe", "secret")
    cursor.description = [("id",), ("status",)]
    cursor.fetchall.return_value = [(1, "pending")]
    mock_connect.return_value = _connect_mock(cursor)

    rows = jobstodo.get_job_details("PROC1", "JOB1", TABLES, "Jupiter")

    assert rows == [{"id": 1, "status": "pending"}]
    # The job must be claimed before any details are read.
    first_call = cursor.execute.call_args_list[0]
    assert "UPDATE" in first_call.args[0]
    assert first_call.args[1:] == ("PROC1", "JOB1")
    executed_sql = [call.args[0] for call in cursor.execute.call_args_list]
    assert any("pending" in sql for sql in executed_sql)
    assert any("ODC_scrape_accounts" in sql for sql in executed_sql)
    assert any("sug_internal_id" in sql for sql in executed_sql)
    assert any("ODC_suppliers" in sql for sql in executed_sql)
    assert any("human_in_loop" in sql for sql in executed_sql)
    assert any("LEFT JOIN" in sql for sql in executed_sql)


@patch("odc_core.db.pyodbc.connect")
def test_get_job_details_multi_credential_job(mock_connect):
    cursor = MagicMock()
    cursor.fetchone.return_value = ("multi_credential", "multi_credential")
    cursor.description = [("id",)]
    cursor.fetchall.return_value = [(1,), (2,)]
    mock_connect.return_value = _connect_mock(cursor)

    rows = jobstodo.get_job_details("PROC1", "JOB1", TABLES, "Jupiter")

    assert len(rows) == 2
    executed_sql = [call.args[0] for call in cursor.execute.call_args_list]
    assert any("NOT IN" in sql for sql in executed_sql)


@patch("odc_core.db.pyodbc.connect")
def test_get_job_details_no_job_row_returns_empty(mock_connect):
    cursor = MagicMock()
    cursor.fetchone.return_value = None
    mock_connect.return_value = _connect_mock(cursor)

    assert jobstodo.get_job_details("PROC1", "JOB1", TABLES, "Jupiter") == []


@patch("odc_core.db.pyodbc.connect")
def test_get_multi_credentials(mock_connect):
    cursor = MagicMock()
    cursor.description = [("username",), ("password",), ("url",)]
    cursor.fetchall.return_value = [("u1", "p1", "http://x")]
    mock_connect.return_value = _connect_mock(cursor)

    rows = jobstodo.get_multi_credentials("C1", "S1", TABLES, "Jupiter")

    assert rows == [{"username": "u1", "password": "p1", "url": "http://x"}]


@patch("odc_core.db.pyodbc.connect")
def test_revert_to_pending_commits(mock_connect):
    cursor = MagicMock()
    conn = _connect_mock(cursor)
    mock_connect.return_value = conn

    jobstodo.revert_to_pending("DETAIL1", TABLES, "Jupiter")

    cursor.execute.assert_called_once()
    conn.commit.assert_called_once()


@patch("odc_core.db.pyodbc.connect")
def test_clear_job_claim_commits(mock_connect):
    cursor = MagicMock()
    conn = _connect_mock(cursor)
    mock_connect.return_value = conn

    jobstodo.clear_job_claim("JOB1", TABLES, "Jupiter")

    cursor.execute.assert_called_once()
    conn.commit.assert_called_once()


@patch("odc_core.db.pyodbc.connect")
def test_set_human_wait_commits_with_status_timeout_rdp_and_job_id(mock_connect):
    cursor = MagicMock()
    conn = _connect_mock(cursor)
    mock_connect.return_value = conn

    jobstodo.set_human_wait("JOB1", 600, "RDS01", "rdpuser", "rdppass", TABLES, "Jupiter")

    cursor.execute.assert_called_once()
    sql, params = cursor.execute.call_args.args[0], cursor.execute.call_args.args[1:]
    assert "human_wait_status" in sql
    assert "human_wait_deadline" in sql
    assert "rdp_host" in sql
    assert "rdp_username" in sql
    assert "rdp_password" in sql
    assert params == (
        jobstodo.HUMAN_WAIT_PENDING, 600, "RDS01", "rdpuser", "rdppass", "JOB1",
    )
    conn.commit.assert_called_once()


@patch("odc_core.db.pyodbc.connect")
def test_set_human_wait_complete_commits_and_nulls_rdp_fields(mock_connect):
    cursor = MagicMock()
    conn = _connect_mock(cursor)
    mock_connect.return_value = conn

    jobstodo.set_human_wait_complete("JOB1", TABLES, "Jupiter")

    cursor.execute.assert_called_once()
    sql, params = cursor.execute.call_args.args[0], cursor.execute.call_args.args[1:]
    assert "human_wait_status" in sql
    assert "rdp_host = NULL" in sql
    assert "rdp_username = NULL" in sql
    assert "rdp_password = NULL" in sql
    assert "human_wait_deadline" not in sql
    assert params == (jobstodo.HUMAN_WAIT_COMPLETE, "JOB1")
    conn.commit.assert_called_once()


@patch("odc_core.db.pyodbc.connect")
def test_clear_human_wait_commits(mock_connect):
    cursor = MagicMock()
    conn = _connect_mock(cursor)
    mock_connect.return_value = conn

    jobstodo.clear_human_wait("JOB1", TABLES, "Jupiter")

    cursor.execute.assert_called_once()
    sql, params = cursor.execute.call_args.args[0], cursor.execute.call_args.args[1:]
    assert "human_wait_status = NULL" in sql
    assert "rdp_host = NULL" in sql
    assert "rdp_username = NULL" in sql
    assert "rdp_password = NULL" in sql
    assert params == ("JOB1",)
    conn.commit.assert_called_once()
