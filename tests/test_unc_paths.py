"""Mapped drive letters are filed through their UNC share (lib-file-allocation's
unc.DRIVE_MAP). Each test points the X: drive at tmp_path, so "X:\\..." is a
real folder only through the translation."""

from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from file_allocation_core import unc

from odc_core import file_allocation, file_save_as, login_failure, pdf_auto_copy, sftp_upload


@pytest.fixture
def x_drive(tmp_path, monkeypatch):
    share = tmp_path / "share"
    share.mkdir()
    monkeypatch.setitem(unc.DRIVE_MAP, "X", str(share))
    return share


def test_allocate_files_through_the_share_and_returns_the_drive_path(x_drive):
    month = date.today().strftime("%Y-%m")
    result = file_allocation.allocate(
        client_name="crown gas", customer_name="Acme", account_reference="ACC1",
        supplier="Crown", client_location=r"X:\POST RECEIVED", utility="Elec",
        meter_number="M1", doc_type="I", bill_date_corrected="2026-10-29",
        file_extension="pdf", invoice_number="INV1", config={},
    )

    assert (x_drive / "POST RECEIVED" / month).is_dir()
    # Recorded as given: ODC_scrape_data.client_file_path keeps the drive letter.
    assert result["folder_location"] == rf"X:\POST RECEIVED\{month}"
    assert result["client_filepath"] == rf"X:\POST RECEIVED\{month}\Crown_ACC1_M1_Elec_INV1_20261029.pdf"


@patch("odc_core.db.pyodbc.connect")
def test_save_moves_to_the_share(mock_connect, x_drive, tmp_path):
    mock_connect.return_value = MagicMock()
    staging = tmp_path / "download.pdf"
    staging.write_bytes(b"x" * 2048)

    file_save_as.save(
        staging_path=str(staging), target_path=r"X:\POST RECEIVED\2026-10\a.pdf",
        job_id="J", job_details_id="D", account_reference="A", unique_file_ref="R",
        bill_date="", bill_date_corrected="", original_filename="o.pdf",
        complete_filename="a", file_extension="pdf", folder_location="",
        client_filepath=r"X:\POST RECEIVED\2026-10\a.pdf", customer_name="C",
        doc_type="INVOICE", download_location="", process_id="P",
        tables={"scrape_data": "ODC_scrape_data"}, dsn="Jupiter",
    )

    assert (x_drive / "POST RECEIVED" / "2026-10" / "a.pdf").is_file()
    inserted = mock_connect.return_value.cursor.return_value.execute.call_args_list[0].args
    assert r"X:\POST RECEIVED\2026-10\a.pdf" in inserted


def test_pdf_auto_copy_translates_source_and_base(x_drive):
    (x_drive / "a.pdf").write_bytes(b"content")

    dest = pdf_auto_copy.copy(r"X:\a.pdf", "Crown", {"pdf_auto": {"base_path": r"X:\ODC"}})

    assert dest == x_drive / "ODC" / "DEV" / "PDFAuto" / "Crown" / "a.pdf"
    assert dest.read_bytes() == b"content"


def test_sftp_staging_translates_paths(x_drive):
    (x_drive / "a.pdf").write_bytes(b"content")
    staging = sftp_upload.staging_dir("Crown", "SSE", {"pdf_auto": {"base_path": r"X:\ODC"}})

    staged = sftp_upload.stage(r"X:\a.pdf", staging)

    assert staged == x_drive / "ODC" / "DEV" / "SFTP" / "Crown" / "SSE" / "a.pdf"
    assert sftp_upload.staged_files(str(staging)) == [staged]


def test_login_failure_notices_dir_translates_base(x_drive):
    assert login_failure.notices_dir({"pdf_auto": {"base_path": r"X:\ODC"}}) == (
        x_drive / "ODC" / "DEV" / login_failure.NOTICES_FOLDER
    )
