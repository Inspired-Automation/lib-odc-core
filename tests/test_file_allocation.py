from unittest.mock import patch

import pytest
from file_allocation_core import routing

from odc_core import file_allocation


def test_normalise_utility_elec():
    assert routing.normalise_utility("Electricity") == "Elec"


def test_normalise_utility_gas():
    assert routing.normalise_utility("GAS") == "Gas"


def test_normalise_utility_passthrough():
    assert routing.normalise_utility("Water") == "Water"


def test_normalise_utility_none():
    assert routing.normalise_utility(None) == ""


def test_allocate_standard_client(tmp_path):
    config = {"file_allocation": {"doc_types": [], "statuses": [], "utilities": []}}

    result = file_allocation.allocate(
        client_name="crown gas",
        customer_name="Acme Ltd",
        account_reference="ACC123",
        supplier="Crown",
        client_location=str(tmp_path),
        utility="Electricity",
        meter_number="M001",
        doc_type="I",
        bill_date_corrected="2026-07-01",
        file_extension="pdf",
        invoice_number="INV1",
        config=config,
    )

    assert result["complete_filename"] == "Crown_ACC123_M001_Elec_INV1_20260701"
    assert result["client_filepath"].endswith(".pdf")
    assert tmp_path.exists()


def test_allocate_standard_client_letter_doc_type(tmp_path):
    config = {"file_allocation": {"doc_types": [], "statuses": [], "utilities": []}}

    result = file_allocation.allocate(
        client_name="crown gas",
        customer_name="Acme Ltd",
        account_reference="ACC123",
        supplier="Crown",
        client_location=str(tmp_path),
        utility="Gas",
        meter_number="M001",
        doc_type="O",
        bill_date_corrected="2026-07-01",
        file_extension="pdf",
        invoice_number="INV1",
        config=config,
    )

    assert result["complete_filename"] == "Crown_ACC123_M001_Gas_INV1_20260701"


def test_allocate_inspired_plc_client(tmp_path):
    config = {
        "file_allocation": {
            "doc_types": ["INVOICE"],
            "statuses": ["NEW"],
            "utilities": ["Elec"],
        }
    }

    result = file_allocation.allocate(
        client_name="Inspired PLC",
        customer_name="Acme Ltd",
        account_reference="ACC123",
        supplier="Crown",
        client_location=str(tmp_path),
        utility="Electricity",
        meter_number="M001",
        doc_type="I",
        bill_date_corrected="2026-07-01",
        file_extension="pdf",
        invoice_number="INV1",
        config=config,
    )

    assert "NEW" in result["folder_location"]
    assert not (tmp_path / "Acme Ltd" / "sugar_id.txt").exists()
    assert (tmp_path / "Acme Ltd" / "INVOICE" / "NEW" / "Elec").exists()


def test_allocate_ignite_client_uses_customer_folder(tmp_path):
    config = {
        "file_allocation": {
            "doc_types": ["INVOICE"],
            "statuses": ["NEW"],
            "utilities": ["Elec"],
        },
        "sugar": {"dsn": "Sugar Corp"},
    }

    result = file_allocation.allocate(
        client_name="Ignite",
        customer_name="Primark",
        account_reference="ACC123",
        supplier="Crown",
        client_location=str(tmp_path),
        utility="Electricity",
        meter_number="M001",
        doc_type="I",
        bill_date_corrected="2026-07-01",
        file_extension="pdf",
        invoice_number="INV1",
        config=config,
        sug_internal_id=None,
    )

    folder = tmp_path / "Primark" / "INVOICE" / "NEW" / "Elec"
    assert result["folder_location"] == str(folder)
    assert result["client_filepath"] == str(folder / "Crown_ACC123_Elec_INV1_20260701.pdf")
    assert folder.exists()
    assert not (tmp_path / "Primark" / "sugar_id.txt").exists()


@patch("odc_core.file_allocation.sugar_client.get_company_name")
def test_allocate_inspired_plc_uses_sugar_company_name(mock_get_company_name, tmp_path):
    mock_get_company_name.return_value = "Real Sugar Company Ltd"
    config = {
        "file_allocation": {
            "doc_types": ["INVOICE"],
            "statuses": ["NEW"],
            "utilities": ["Elec"],
        },
        "sugar": {"dsn": "Sugar Corp"},
    }

    result = file_allocation.allocate(
        client_name="Inspired PLC",
        customer_name="Acme Ltd",
        account_reference="ACC123",
        supplier="Crown",
        client_location=str(tmp_path),
        utility="Electricity",
        meter_number="M001",
        doc_type="I",
        bill_date_corrected="2026-07-01",
        file_extension="pdf",
        invoice_number="INV1",
        config=config,
        sug_internal_id="SUGAR123",
    )

    mock_get_company_name.assert_called_once_with("SUGAR123", "Sugar Corp")
    assert "Real Sugar Company Ltd" in result["folder_location"]
    assert "Acme Ltd" not in result["folder_location"]
    assert not (tmp_path / "Acme Ltd").exists()
    assert not (tmp_path / "Real Sugar Company Ltd" / "sugar_id.txt").exists()


@patch("odc_core.file_allocation.sugar_client.get_company_name")
def test_allocate_inspired_plc_falls_back_when_sugar_lookup_misses(mock_get_company_name, tmp_path):
    mock_get_company_name.return_value = None
    config = {
        "file_allocation": {
            "doc_types": ["INVOICE"],
            "statuses": ["NEW"],
            "utilities": ["Elec"],
        },
        "sugar": {"dsn": "Sugar Corp"},
    }

    result = file_allocation.allocate(
        client_name="Inspired PLC",
        customer_name="Acme Ltd",
        account_reference="ACC123",
        supplier="Crown",
        client_location=str(tmp_path),
        utility="Electricity",
        meter_number="M001",
        doc_type="I",
        bill_date_corrected="2026-07-01",
        file_extension="pdf",
        invoice_number="INV1",
        config=config,
        sug_internal_id="SUGAR123",
    )

    assert "Acme Ltd" in result["folder_location"]


INSPIRED_CONFIG = {
    "file_allocation": {"doc_types": ["INVOICE"], "statuses": ["NEW"], "utilities": ["Elec"]},
    "sugar": {"dsn": "Sugar Corp"},
    "database": {"dsn": "Jupiter"},
}


def _allocate_inspired(tmp_path, config, sug_internal_id="SUGAR123"):
    return file_allocation.allocate(
        client_name="Inspired PLC",
        customer_name="Acme Ltd",
        account_reference="ACC123",
        supplier="Crown",
        client_location=str(tmp_path),
        utility="Electricity",
        meter_number="M001",
        doc_type="I",
        bill_date_corrected="2026-07-01",
        file_extension="pdf",
        invoice_number="INV1",
        config=config,
        sug_internal_id=sug_internal_id,
    )


@patch("odc_core.file_allocation.sugar_client.get_company_name", return_value="Sugar Name Ltd")
@patch("file_allocation_core.routing.resolve_customer_folder", return_value="Folder The Table Holds")
def test_prod_run_files_into_customer_master_folder(mock_resolve, _mock_name, tmp_path):
    result = _allocate_inspired(tmp_path, {**INSPIRED_CONFIG, "env": "prod"})

    mock_resolve.assert_called_once_with(
        tmp_path, "SUGAR123", "Sugar Name Ltd", "odc", dsn="Jupiter"
    )
    assert result["folder_location"] == str(
        tmp_path / "Folder The Table Holds" / "INVOICE" / "NEW" / "Elec"
    )


@patch("odc_core.file_allocation.sugar_client.get_company_name", return_value="Sugar Name Ltd")
@patch("file_allocation_core.routing.resolve_customer_folder")
def test_dev_run_never_touches_customer_master(mock_resolve, _mock_name, tmp_path):
    result = _allocate_inspired(tmp_path, {**INSPIRED_CONFIG, "env": "dev"})

    mock_resolve.assert_not_called()
    assert "Sugar Name Ltd" in result["folder_location"]


@patch("odc_core.file_allocation.sugar_client.get_company_name", return_value="A/B: Co")
def test_sugar_name_made_safe_for_windows(_mock_name, tmp_path):
    result = _allocate_inspired(tmp_path, INSPIRED_CONFIG)
    assert result["folder_location"] == str(tmp_path / "AB- Co" / "INVOICE" / "NEW" / "Elec")


def test_blank_customer_name_is_not_filed_in_root(tmp_path):
    config = {"file_allocation": {"doc_types": ["INVOICE"], "statuses": ["NEW"], "utilities": []}}
    with pytest.raises(ValueError):
        file_allocation.allocate(
            client_name="Ignite", customer_name="", account_reference="A", supplier="S",
            client_location=str(tmp_path), utility="Gas", meter_number="M", doc_type="I",
            bill_date_corrected="2026-07-01", file_extension="pdf", invoice_number="I",
            config=config,
        )
