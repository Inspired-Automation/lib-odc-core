from unittest.mock import MagicMock, patch

from odc_core import grab_all

TABLES = {
    "jobs": "ODC_jobs",
    "grab_all_data": "ODC_grab_all_data",
    "suppliers": "ODC_suppliers",
}


def _connect_mock(row, columns=None):
    cursor = MagicMock()
    cursor.execute.return_value.fetchone.return_value = row
    cursor.fetchone.return_value = row
    cursor.description = [(c,) for c in (columns or [])]
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value = cursor
    return conn, cursor


@patch("odc_core.db.pyodbc.connect")
def test_is_duplicate_true(mock_connect):
    conn, cursor = _connect_mock((1,))
    mock_connect.return_value = conn

    result = grab_all.is_duplicate("ACC1", "SSE Airtricity", "BoxFIsh", "REF1", TABLES, "Jupiter")

    assert result is True
    sql = cursor.execute.call_args.args[0]
    assert "ODC_grab_all_data" in sql
    assert "ODC_scrape_data" not in sql
    assert cursor.execute.call_args.args[1:] == ("ACC1", "SSE Airtricity", "BoxFIsh", "REF1")


@patch("odc_core.db.pyodbc.connect")
def test_is_duplicate_false(mock_connect):
    conn, _ = _connect_mock((0,))
    mock_connect.return_value = conn

    assert grab_all.is_duplicate("ACC1", "SSE", "BoxFIsh", "REF1", TABLES, "Jupiter") is False


@patch("odc_core.db.pyodbc.connect")
def test_get_job_maps_columns(mock_connect):
    columns = ["id", "grab_all", "grab_all_start_date", "client_name"]
    conn, cursor = _connect_mock((148, True, None, "BoxFIsh"), columns)
    mock_connect.return_value = conn

    job = grab_all.get_job("148", TABLES, "Jupiter")

    assert job == {"id": 148, "grab_all": True, "grab_all_start_date": None, "client_name": "BoxFIsh"}
    assert grab_all.is_grab_all_job(job) is True
    sql = cursor.execute.call_args.args[0]
    assert "FROM ODC_jobs AS j" in sql
    assert "LEFT JOIN ODC_suppliers AS s" in sql
    assert "s.[human_in_loop]" in sql


@patch("odc_core.db.pyodbc.connect")
def test_get_job_includes_human_in_loop(mock_connect):
    columns = ["id", "grab_all", "grab_all_start_date", "client_name", "human_in_loop"]
    conn, _ = _connect_mock((148, True, None, "BoxFIsh", True), columns)
    mock_connect.return_value = conn

    job = grab_all.get_job("148", TABLES, "Jupiter")

    assert job["human_in_loop"] is True


@patch("odc_core.db.pyodbc.connect")
def test_get_job_missing_returns_none(mock_connect):
    conn, _ = _connect_mock(None)
    mock_connect.return_value = conn

    job = grab_all.get_job("999", TABLES, "Jupiter")

    assert job is None
    assert grab_all.is_grab_all_job(job) is False


def test_is_grab_all_job_false_when_flag_zero():
    assert grab_all.is_grab_all_job({"grab_all": 0}) is False


@patch("odc_core.db.pyodbc.connect")
def test_claim_job_stamps_process_id(mock_connect):
    conn, cursor = _connect_mock(None)
    mock_connect.return_value = conn

    grab_all.claim_job("148", "PROC1", TABLES, "Jupiter")

    args = cursor.execute.call_args.args
    assert args[0] == "UPDATE ODC_jobs SET process_id = ? WHERE id = ?"
    assert args[1:] == ("PROC1", "148")
    conn.commit.assert_called_once()


def _allocate_kwargs(**overrides):
    kwargs = dict(
        client_name="BoxFIsh",
        customer_name="Acme",
        account_reference="ACC1",
        supplier="SSE Airtricity",
        client_location="C:/out",
        utility="Electricity",
        meter_number="10012345678",
        doc_type="INVOICE",
        bill_date_corrected="2026-07-27",
        file_extension="pdf",
        invoice_number="55057066",
        config={},
    )
    kwargs.update(overrides)
    return kwargs


@patch("odc_core.grab_all.file_allocation.allocate")
def test_allocate_passes_values_through(mock_allocate):
    mock_allocate.return_value = {"client_filepath": "x"}

    result = grab_all.allocate(**_allocate_kwargs())

    assert result == {"client_filepath": "x"}
    called = mock_allocate.call_args.kwargs
    assert called["meter_number"] == "10012345678"
    assert called["utility"] == "Electricity"
    assert called["sug_internal_id"] is None


@patch("odc_core.grab_all.file_allocation.allocate")
def test_allocate_substitutes_unknown_for_blank_meter_and_utility(mock_allocate):
    mock_allocate.return_value = {}

    grab_all.allocate(**_allocate_kwargs(meter_number=" ", utility=None))

    called = mock_allocate.call_args.kwargs
    assert called["meter_number"] == grab_all.UNKNOWN
    assert called["utility"] == grab_all.UNKNOWN


def _save_kwargs(staging_path, target_path):
    return dict(
        staging_path=str(staging_path),
        target_path=str(target_path),
        job_id="148",
        account_reference="ACC1",
        unique_file_ref="55057066",
        bill_date="27 Jul 2026",
        bill_date_corrected="2026-07-27",
        original_filename="bill.pdf",
        complete_filename="SSE_ACC1_10012345678_Elec_55057066_20260727",
        file_extension="pdf",
        client_filepath="out/2026-09/x.pdf",
        customer_name="Acme",
        doc_type="I",
        download_location="C:/staging",
        process_id="PROC1",
        tables=TABLES,
        dsn="Jupiter",
    )


def _save_connect_mock(new_id=42):
    cursor = MagicMock()
    cursor.fetchone.return_value = (new_id,)
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value = cursor
    return conn, cursor


@patch("odc_core.db.pyodbc.connect")
def test_save_inserts_grab_all_columns_only(mock_connect, tmp_path):
    staging = tmp_path / "bill.pdf"
    staging.write_bytes(b"x" * 2048)
    target = tmp_path / "out" / "x.pdf"
    conn, cursor = _save_connect_mock()
    mock_connect.return_value = conn

    assert grab_all.save(**_save_kwargs(staging, target)) is True

    assert target.exists() and not staging.exists()
    assert cursor.execute.call_count == 1  # INSERT only, no VOID
    sql, *bound = cursor.execute.call_args.args
    assert "INSERT INTO ODC_grab_all_data" in sql
    assert "OUTPUT INSERTED.[id]" in sql
    for absent in ("job_details_id", "account_type", "zip_address"):
        assert absent not in sql
    assert bound[0] == "148" and bound[1] == "Acme" and len(bound) == 13


@patch("odc_core.db.pyodbc.connect")
def test_save_small_file_voids_by_inserted_id(mock_connect, tmp_path):
    staging = tmp_path / "bill.pdf"
    staging.write_bytes(b"x" * 100)
    target = tmp_path / "out" / "x.pdf"
    conn, cursor = _save_connect_mock(new_id=7)
    mock_connect.return_value = conn

    grab_all.save(**_save_kwargs(staging, target))

    assert cursor.execute.call_count == 2
    void_sql, void_id = cursor.execute.call_args_list[1].args
    assert "SET [status] = 'VOID'" in void_sql
    assert "WHERE [id] = ?" in void_sql
    assert void_id == 7


@patch("odc_core.db.pyodbc.connect")
def test_save_binds_raw_apostrophes(mock_connect, tmp_path):
    staging = tmp_path / "bill.pdf"
    staging.write_bytes(b"x" * 2048)
    conn, cursor = _save_connect_mock()
    mock_connect.return_value = conn

    kwargs = _save_kwargs(staging, tmp_path / "out" / "x.pdf")
    kwargs["customer_name"] = "O'Brien's"
    grab_all.save(**kwargs)

    bound = cursor.execute.call_args.args[1:]
    assert "O'Brien's" in bound
    assert not any(isinstance(v, str) and "''" in v for v in bound)
