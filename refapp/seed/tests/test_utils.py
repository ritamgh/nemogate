from app.utils import collapse_spaces, decode_meta


def test_collapse_spaces():
    assert collapse_spaces("  a \n b\t\tc ") == "a b c"


def test_decode_meta_reads_objects():
    assert decode_meta('{"due": "2026-03-01"}') == {"due": "2026-03-01"}


def test_decode_meta_falls_back_to_empty():
    assert decode_meta(None) == {}
    assert decode_meta("") == {}
    assert decode_meta("not json") == {}
    assert decode_meta("[1, 2]") == {}
