from pipeline.core.hashing import hash_row


def test_hash_row_is_order_independent():
    a = hash_row({"x": 1, "y": 2})
    b = hash_row({"y": 2, "x": 1})
    assert a == b


def test_hash_row_changes_with_value():
    a = hash_row({"x": 1})
    b = hash_row({"x": 2})
    assert a != b
