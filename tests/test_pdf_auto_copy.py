from unittest.mock import patch

import pytest

from odc_core import pdf_auto_copy


def _source(tmp_path, content=b"content"):
    source = tmp_path / "invoice.pdf"
    source.write_bytes(content)
    return source


def test_copy_defaults_to_dev_folder_when_env_absent(tmp_path):
    source = _source(tmp_path)
    base = tmp_path / "base"

    dest = pdf_auto_copy.copy(
        str(source), "Crown Gas", {"pdf_auto": {"base_path": str(base)}}
    )

    assert dest == base / "DEV" / "PDFAuto" / "Crown Gas" / "invoice.pdf"
    assert dest.read_bytes() == b"content"


@pytest.mark.parametrize("env, folder", [
    ("prod", "LIVE"),   # what every supplier bot sets in production
    ("live", "LIVE"),
    ("PROD", "LIVE"),
    ("dev", "DEV"),
])
def test_copy_maps_env_to_the_folder_the_proc_queues(tmp_path, env, folder):
    source = _source(tmp_path)
    base = tmp_path / "base"

    dest = pdf_auto_copy.copy(
        str(source), "Crown Gas", {"env": env, "pdf_auto": {"base_path": str(base)}}
    )

    assert dest == base / folder / "PDFAuto" / "Crown Gas" / "invoice.pdf"
    assert dest.is_file()


def test_copy_rejects_unknown_env(tmp_path):
    source = _source(tmp_path)

    with pytest.raises(ValueError, match="unknown env"):
        pdf_auto_copy.copy(
            str(source), "Crown Gas",
            {"env": "uat", "pdf_auto": {"base_path": str(tmp_path / "base")}},
        )
    assert not (tmp_path / "base").exists()


def test_copy_raises_when_destination_missing_after_copy(tmp_path):
    source = _source(tmp_path)

    with patch("odc_core.pdf_auto_copy.shutil.copy2"):   # copy silently does nothing
        with pytest.raises(pdf_auto_copy.PdfAutoCopyError, match="not found"):
            pdf_auto_copy.copy(
                str(source), "Crown Gas",
                {"env": "prod", "pdf_auto": {"base_path": str(tmp_path / "base")}},
            )


def test_copy_raises_on_size_mismatch(tmp_path):
    source = _source(tmp_path, b"full content")

    def truncated_copy(src, dst):
        open(dst, "wb").write(b"full")

    with patch("odc_core.pdf_auto_copy.shutil.copy2", side_effect=truncated_copy):
        with pytest.raises(pdf_auto_copy.PdfAutoCopyError, match="size mismatch"):
            pdf_auto_copy.copy(
                str(source), "Crown Gas",
                {"env": "prod", "pdf_auto": {"base_path": str(tmp_path / "base")}},
            )


def test_copy_error_is_an_oserror():
    # Callers that already catch OSError around the save keep working.
    assert issubclass(pdf_auto_copy.PdfAutoCopyError, OSError)


@patch("odc_core.pdf_auto_copy._verify", return_value=7)
@patch("odc_core.pdf_auto_copy.shutil.copy2")
@patch("odc_core.pdf_auto_copy.Path.mkdir")
def test_copy_falls_back_to_default_base_path(mock_mkdir, mock_copy2, mock_verify):
    # No pdf_auto key at all: must land under _DEFAULT_BASE. Filesystem calls
    # are mocked so this never touches the real share.
    dest = pdf_auto_copy.copy(r"C:\staging\invoice.pdf", "Crown Gas", {"env": "prod"})

    assert str(dest).startswith(pdf_auto_copy._DEFAULT_BASE)
    assert str(dest).startswith("\\\\inspiredenergysolutions.local\\")
    assert dest.parts[-4:] == ("LIVE", "PDFAuto", "Crown Gas", "invoice.pdf")
    mock_copy2.assert_called_once()
