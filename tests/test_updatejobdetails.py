from unittest.mock import MagicMock, patch

import pytest

from odc_core import updatejobdetails

TABLES = {"db_name": "Titan_INSE"}


@patch("odc_core.updatejobdetails.pyodbc.connect")
def test_update_valid_status_executes_and_commits(mock_connect):
    cursor = MagicMock()
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value = cursor
    mock_connect.return_value = conn

    updatejobdetails.update("DETAIL1", "DOWNLOADED", True, TABLES, "Jupiter")

    cursor.execute.assert_called_once()
    args = cursor.execute.call_args.args
    assert args[1:] == ("DETAIL1", "DOWNLOADED", "true")
    conn.commit.assert_called_once()


@patch("odc_core.updatejobdetails.pyodbc.connect")
def test_update_invalid_status_raises_without_connecting(mock_connect):
    with pytest.raises(ValueError):
        updatejobdetails.update("DETAIL1", "BOGUS", False, TABLES, "Jupiter")

    mock_connect.assert_not_called()
