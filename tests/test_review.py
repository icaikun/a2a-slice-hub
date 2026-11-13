from a2a_slice_hub.review import schema_compatible


def test_schema_annotations_can_change():
    assert schema_compatible({"type": "string", "description": "old"}, {"type": "string", "description": "new"})


def test_property_named_description_is_not_ignored():
    before = {"properties": {"description": {"type": "string"}}}
    after = {"properties": {"description": {"type": "integer"}}}
    assert not schema_compatible(before, after)


def test_const_and_unknown_keyword_values_are_not_stripped():
    for key in ("const", "enum", "x-custom-validator"):
        assert not schema_compatible({key: {"description": "old"}}, {key: {"description": "new"}})
