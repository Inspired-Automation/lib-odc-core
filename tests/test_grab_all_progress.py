from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from odc_core import grab_all_progress as gp

TABLES = {
    "grab_all_cycle": "ODC_grab_all_cycle",
    "grab_all_progress": "ODC_grab_all_progress",
}
STARTED = datetime(2026, 10, 20, 7, 48)


def _connect_mock(fetchone=(), fetchall=(), rowcount=1):
    cursor = MagicMock()
    cursor.execute.return_value = cursor
    cursor.fetchone.side_effect = list(fetchone)
    cursor.fetchall.return_value = list(fetchall)
    cursor.rowcount = rowcount
    conn = MagicMock()
    conn.cursor.return_value = cursor
    return conn, cursor


def _cycle(**statuses):
    cycle = gp.Cycle(
        id=7, supplier="Electric Ireland", client_name="Horizon",
        login_username="a@b.ie", started_at_utc=STARTED, resumed=True,
    )
    cycle.statuses.update(statuses)
    return cycle


@patch("odc_core.db.pyodbc.connect")
def test_open_cycle_resumes_existing(mock_connect):
    conn, cursor = _connect_mock(
        fetchone=[(7, STARTED)],
        fetchall=[("BANK OF IRELAND GROUP", "", "DONE"), ("Horizon - Northern Ireland", "210001115", "DONE"),
                  ("Horizon - NIDOF", "", "FAILED")],
    )
    mock_connect.return_value = conn

    cycle = gp.open_cycle(" Electric Ireland ", "Horizon", "a@b.ie", "4996", TABLES, "Jupiter")

    assert cycle.id == 7 and cycle.resumed is True
    assert cycle.supplier == "Electric Ireland"
    sqls = [c.args[0] for c in cursor.execute.call_args_list]
    assert not any("INSERT" in s for s in sqls)
    assert "WITH (UPDLOCK, HOLDLOCK)" in sqls[0]
    assert cursor.execute.call_args_list[0].args[1:] == ("Electric Ireland", "Horizon", "a@b.ie")
    assert cycle.is_done("bank of ireland   group")
    assert cycle.is_done("Horizon - Northern Ireland", "210001115")
    assert not cycle.is_done("Horizon - Northern Ireland", "210010681")
    assert not cycle.is_done("Horizon - NIDOF")  # FAILED is always retried
    assert cycle.counts() == {"DONE": 2, "FAILED": 1}


@patch("odc_core.db.pyodbc.connect")
def test_open_cycle_starts_new_when_none_open(mock_connect):
    conn, cursor = _connect_mock(fetchone=[None, (8, STARTED)])
    mock_connect.return_value = conn

    cycle = gp.open_cycle("Electric Ireland", "Horizon", "a@b.ie", "4996", TABLES, "Jupiter")

    assert cycle.id == 8 and cycle.resumed is False
    insert = cursor.execute.call_args_list[1]
    assert insert.args[0].strip().startswith("INSERT INTO ODC_grab_all_cycle")
    assert insert.args[1:] == ("Electric Ireland", "Horizon", "a@b.ie", 4996)
    conn.commit.assert_called_once()
    assert cycle.statuses == {}


@patch("odc_core.db.pyodbc.connect")
def test_record_updates_existing_row(mock_connect):
    conn, cursor = _connect_mock(rowcount=1)
    mock_connect.return_value = conn
    cycle = _cycle()

    gp.record(cycle, "Horizon - NIDOF", "", gp.STATUS_DONE, job_id="4996", process_id=14993, tables=TABLES, dsn="Jupiter")

    assert cursor.execute.call_count == 1
    assert cursor.execute.call_args.args[0].strip().startswith("UPDATE ODC_grab_all_progress")
    assert cursor.execute.call_args.args[1:] == ("DONE", 0, 4996, "14993", 7, "Horizon - NIDOF", "")
    assert cycle.is_done("Horizon - NIDOF")


@patch("odc_core.db.pyodbc.connect")
def test_record_inserts_when_no_row(mock_connect):
    conn, cursor = _connect_mock(rowcount=0)
    mock_connect.return_value = conn
    cycle = _cycle()

    gp.record(cycle, "  Halfords  Two ", " 210014292 ", gp.STATUS_FAILED, bills_saved=2,
              job_id=4996, process_id=None, tables=TABLES, dsn="Jupiter")

    insert = cursor.execute.call_args_list[1]
    assert insert.args[0].strip().startswith("INSERT INTO ODC_grab_all_progress")
    assert insert.args[1:] == (7, "Halfords Two", "210014292", "FAILED", 2, 4996, None)
    conn.commit.assert_called_once()
    assert cycle.status_of("halfords two", "210014292") == "FAILED"


def test_record_rejects_unknown_status():
    with pytest.raises(ValueError):
        gp.record(_cycle(), "X", "", "SKIPPED", job_id=1, process_id=1, tables=TABLES, dsn="Jupiter")


@patch("odc_core.db.pyodbc.connect")
def test_close_cycle_only_closes_open_cycle(mock_connect):
    conn, cursor = _connect_mock()
    mock_connect.return_value = conn

    gp.close_cycle(_cycle(), "4997", TABLES, "Jupiter")

    sql = cursor.execute.call_args.args[0]
    assert "UPDATE ODC_grab_all_cycle" in sql
    assert "[completed_at_utc] IS NULL" in sql
    assert cursor.execute.call_args.args[1:] == (4997, 7)
    conn.commit.assert_called_once()


def test_skip_statuses():
    cycle = _cycle(**{})
    cycle.statuses[("a", "")] = gp.STATUS_NO_ACCOUNTS
    cycle.statuses[("b", "")] = gp.STATUS_NOT_AUTHORISED
    assert cycle.is_done("A") and cycle.is_done("b")
    assert cycle.status_of("c") is None and not cycle.is_done("c")
