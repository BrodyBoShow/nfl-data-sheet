from pipeline.core.base import _resolve_skip_status


def test_true_means_proceed():
    assert _resolve_skip_status(True) is None


def test_false_is_generic_skipped_fresh():
    assert _resolve_skip_status(False) == "skipped_fresh"


def test_string_is_passed_through_verbatim():
    assert _resolve_skip_status("skipped_no_prior") == "skipped_no_prior"
