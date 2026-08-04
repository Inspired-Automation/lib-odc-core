from unittest.mock import MagicMock, patch

import pyodbc
import pytest

from odc_core import db


def _transient(sqlstate="08001"):
    return pyodbc.OperationalError(
        sqlstate,
        f"[{sqlstate}] [Microsoft][ODBC SQL Server Driver][DBNETLIB]"
        "SQL Server does not exist or access denied.",
    )


def _permanent():
    return pyodbc.ProgrammingError(
        "42S02",
        "[42S02] [Microsoft][ODBC SQL Server Driver][SQL Server]"
        "Invalid object name 'Titan_INSE.dbo.Nope'.",
    )


def test_sqlstate_reads_args_zero():
    assert db.sqlstate(_transient("hyt00")) == "HYT00"
    assert db.sqlstate(RuntimeError()) == ""


def test_is_transient_only_for_listed_sqlstates():
    for state in db.TRANSIENT_SQLSTATES:
        assert db.is_transient(_transient(state))
    assert not db.is_transient(_permanent())
    assert not db.is_transient(RuntimeError("08001"))


@patch("odc_core.db.time.sleep", return_value=None)
@patch("odc_core.db.pyodbc.connect")
def test_run_retries_transient_connect_then_succeeds(mock_connect, mock_sleep):
    conn = MagicMock()
    mock_connect.side_effect = [_transient(), _transient(), conn]

    result = db.run("Jupiter", lambda c: "ok", description="probe")

    assert result == "ok"
    assert mock_connect.call_count == 3
    assert mock_sleep.call_count == 2
    conn.close.assert_called_once()


@patch("odc_core.db.time.sleep", return_value=None)
@patch("odc_core.db.pyodbc.connect")
def test_run_retries_a_transient_failure_inside_the_work(mock_connect, mock_sleep):
    conn = MagicMock()
    mock_connect.return_value = conn
    calls = []

    def work(_conn):
        calls.append(1)
        if len(calls) < 2:
            raise _transient("40001")
        return "second time"

    assert db.run("Jupiter", work, description="probe") == "second time"
    assert len(calls) == 2
    # A fresh connection per attempt, each one closed.
    assert mock_connect.call_count == 2
    assert conn.close.call_count == 2


@patch("odc_core.db.time.sleep", return_value=None)
@patch("odc_core.db.pyodbc.connect")
def test_run_raises_the_last_error_when_attempts_are_exhausted(mock_connect, mock_sleep):
    mock_connect.side_effect = _transient("HYT00")

    with pytest.raises(pyodbc.OperationalError) as excinfo:
        db.run("Jupiter", lambda c: "never", description="probe", attempts=3)

    assert db.sqlstate(excinfo.value) == "HYT00"
    assert mock_connect.call_count == 3
    assert mock_sleep.call_count == 2


@patch("odc_core.db.time.sleep", return_value=None)
@patch("odc_core.db.pyodbc.connect")
def test_run_does_not_retry_a_non_transient_error(mock_connect, mock_sleep):
    conn = MagicMock()
    mock_connect.return_value = conn

    def work(_conn):
        raise _permanent()

    with pytest.raises(pyodbc.ProgrammingError):
        db.run("Jupiter", work, description="probe")

    assert mock_connect.call_count == 1
    mock_sleep.assert_not_called()
    conn.close.assert_called_once()


@patch("odc_core.db.pyodbc.connect")
def test_run_uses_a_trusted_dsn_only_connection_string(mock_connect):
    mock_connect.return_value = MagicMock()

    db.run("Jupiter", lambda c: None)

    mock_connect.assert_called_once_with("DSN=Jupiter")


def test_run_rejects_a_zero_attempt_budget():
    with pytest.raises(ValueError):
        db.run("Jupiter", lambda c: None, attempts=0)


@patch("odc_core.db.time.sleep", return_value=None)
@patch("odc_core.db.pyodbc.connect")
def test_connect_retries_the_connect_and_closes_after_the_block(mock_connect, mock_sleep):
    conn = MagicMock()
    mock_connect.side_effect = [_transient("08S01"), conn]

    with db.connect("Jupiter") as handle:
        assert handle is conn
        conn.close.assert_not_called()

    conn.close.assert_called_once()
    assert mock_connect.call_count == 2


@patch("odc_core.db.pyodbc.connect")
def test_connect_closes_even_when_the_block_raises(mock_connect):
    conn = MagicMock()
    mock_connect.return_value = conn

    with pytest.raises(RuntimeError):
        with db.connect("Jupiter"):
            raise RuntimeError("boom")

    conn.close.assert_called_once()
