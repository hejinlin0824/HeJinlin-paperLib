"""Manual CLI helpers must be import-safe during pytest collection."""
from pathlib import Path
import runpy


ROOT = Path(__file__).resolve().parents[1]


def test_api_smoke_script_does_not_execute_on_import():
    namespace = runpy.run_path(str(ROOT / "tools" / "api_test.py"),
                               run_name="paperlib_test_import")
    assert callable(namespace["main"])


def test_parser_smoke_script_does_not_parse_pdf_on_import():
    namespace = runpy.run_path(str(ROOT / "tools" / "test_parser.py"),
                               run_name="paperlib_test_import")
    assert callable(namespace["main"])
