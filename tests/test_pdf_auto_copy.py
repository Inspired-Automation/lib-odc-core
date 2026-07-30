from unittest.mock import patch

from odc_core import pdf_auto_copy


def test_copy_defaults_to_dev_env_when_env_absent(tmp_path):
    source = tmp_path / "invoice.pdf"
    source.write_bytes(b"content")
    base = tmp_path / "base"

    dest = pdf_auto_copy.copy(
        str(source), "Crown Gas", {"pdf_auto": {"base_path": str(base)}}
    )

    assert dest == base / "dev" / "PDFAuto" / "Crown Gas" / "invoice.pdf"
    assert dest.exists()
    assert dest.read_bytes() == b"content"


@patch("odc_core.pdf_auto_copy.shutil.copy2")
@patch("odc_core.pdf_auto_copy.Path.mkdir")
def test_copy_falls_back_to_default_base_path(mock_mkdir, mock_copy2):
    # No pdf_auto key at all: must land under _DEFAULT_BASE. Filesystem calls
    # are mocked so this never touches the real I: drive.
    dest = pdf_auto_copy.copy(r"C:\staging\invoice.pdf", "Crown Gas", {})

    assert str(dest).startswith(pdf_auto_copy._DEFAULT_BASE)
    assert dest.parts[-3:] == ("PDFAuto", "Crown Gas", "invoice.pdf")
    mock_copy2.assert_called_once()


def test_copy_uses_configured_env(tmp_path):
    source = tmp_path / "invoice.pdf"
    source.write_bytes(b"content")
    base = tmp_path / "base"

    dest = pdf_auto_copy.copy(
        str(source),
        "Crown Gas",
        {"env": "live", "pdf_auto": {"base_path": str(base)}},
    )

    assert dest == base / "live" / "PDFAuto" / "Crown Gas" / "invoice.pdf"
