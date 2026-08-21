from unittest.mock import patch

from odc_core import file_allocation


def test_normalise_utility_elec():
    assert file_allocation._normalise_utility("Electricity") == "Elec"


def test_normalise_utility_gas():
    assert file_allocation._normalise_utility("GAS") == "Gas"


def test_normalise_utility_passthrough():
    assert file_allocation._normalise_utility("Water") == "Water"


def test_normalise_utility_none():
    assert file_allocation._normalise_utility(None) == ""


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
    assert (tmp_path / "Acme Ltd" / "sugar_id.txt").exists()
    assert (tmp_path / "Acme Ltd" / "INVOICE" / "NEW" / "Elec").exists()


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
    sugar_id_file = tmp_path / "Real Sugar Company Ltd" / "sugar_id.txt"
    assert sugar_id_file.exists()
    assert sugar_id_file.read_text(encoding="ansi") == "SUGAR123"


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
