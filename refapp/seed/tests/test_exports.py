import pytest
from app.exports import get_exporter


def test_csv_export_has_header_and_quotes_commas():
    rows = [
        {"id": 1, "title": "Plan, then build", "descripton": None, "done": False},
        {"id": 2, "title": "Ship", "descripton": "v1", "done": True},
    ]

    text = get_exporter("csv")(rows)

    assert text == 'id,title,descripton,done\n1,"Plan, then build",,False\n2,Ship,v1,True\n'


def test_unknown_export_format_is_rejected():
    with pytest.raises(ValueError, match="unknown export format: 'xml'"):
        get_exporter("xml")
