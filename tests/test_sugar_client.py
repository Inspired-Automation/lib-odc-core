from unittest.mock import MagicMock, patch

from odc_core import sugar_client


def _connect_mock(cursor):
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value = cursor
    return conn


@patch("odc_core.db.pyodbc.connect")
def test_get_company_name_returns_value(mock_connect):
    cursor = MagicMock()
    cursor.fetchone.return_value = ("Real Sugar Company Ltd",)
    mock_connect.return_value = _connect_mock(cursor)

    assert sugar_client.get_company_name("SUGAR123", "Sugar Corp") == "Real Sugar Company Ltd"
    cursor.execute.assert_called_once_with(sugar_client._SELECT_ACCOUNT_NAME, "SUGAR123")


@patch("odc_core.db.pyodbc.connect")
def test_get_company_name_returns_none_when_missing(mock_connect):
    cursor = MagicMock()
    cursor.fetchone.return_value = None
    mock_connect.return_value = _connect_mock(cursor)

    assert sugar_client.get_company_name("SUGAR123", "Sugar Corp") is None
