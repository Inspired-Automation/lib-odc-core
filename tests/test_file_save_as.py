from unittest.mock import MagicMock, patch

from odc_core import file_save_as

TABLES = {"scrape_data": "ODC_scrape_data"}


def _connect_mock(cursor):
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value = cursor
    return conn


def _save_kwargs(staging_path, target_path):
    return dict(
        staging_path=str(staging_path),
        target_path=str(target_path),
        job_id="JOB1",
        job_details_id="DETAIL1",
        account_reference="ACC1",
        unique_file_ref="REF1",
        bill_date="2026-07-01",
        bill_date_corrected="2026-07-01",
        original_filename="invoice.pdf",
        complete_filename="invoice",
        file_extension="pdf",
        folder_location="folder",
        client_filepath="folder/invoice.pdf",
        customer_name="Acme",
        doc_type="INVOICE",
        download_location="portal",
        process_id="PROC1",
        tables=TABLES,
        dsn="Jupiter",
    )


@patch("odc_core.file_save_as.pyodbc.connect")
def test_save_normal_file_not_marked_void(mock_connect, tmp_path):
    staging = tmp_path / "staging.pdf"
    staging.write_bytes(b"x" * 2048)
    target = tmp_path / "target" / "invoice.pdf"

    cursor = MagicMock()
    conn = _connect_mock(cursor)
    mock_connect.return_value = conn

    result = file_save_as.save(**_save_kwargs(staging, target))

    assert result is True
    assert target.exists()
    assert cursor.execute.call_count == 1  # only the INSERT, no VOID update


@patch("odc_core.file_save_as.pyodbc.connect")
def test_save_binds_raw_values_without_escaping_apostrophes(mock_connect, tmp_path):
    # pyodbc escapes bound parameters itself. Pre-escaping here would store the
    # literal doubled quote, so the values must reach execute() untouched.
    staging = tmp_path / "staging.pdf"
    staging.write_bytes(b"x" * 2048)
    target = tmp_path / "target" / "invoice.pdf"

    cursor = MagicMock()
    mock_connect.return_value = _connect_mock(cursor)

    kwargs = _save_kwargs(staging, target)
    kwargs["customer_name"] = "Sainsbury's"
    kwargs["original_filename"] = "O'Brien invoice.pdf"

    file_save_as.save(**kwargs)

    bound = cursor.execute.call_args.args[1:]
    assert "Sainsbury's" in bound
    assert "O'Brien invoice.pdf" in bound
    assert not any(isinstance(v, str) and "''" in v for v in bound)


@patch("odc_core.file_save_as.pyodbc.connect")
def test_save_small_file_marked_void(mock_connect, tmp_path):
    staging = tmp_path / "staging.pdf"
    staging.write_bytes(b"x" * 100)
    target = tmp_path / "target" / "invoice.pdf"

    cursor = MagicMock()
    conn = _connect_mock(cursor)
    mock_connect.return_value = conn

    result = file_save_as.save(**_save_kwargs(staging, target))

    assert result is True
    assert cursor.execute.call_count == 2  # INSERT + VOID update
    void_sql = cursor.execute.call_args_list[1].args[0]
    assert "VOID" in void_sql
