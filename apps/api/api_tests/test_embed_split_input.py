"""P1.C5: the API sets `embed_split: true` on new kb_ingest runs; a run created before (no flag)
is still recognised as the run of the same document version or generation."""

from eap_api.routes.knowledge import _same_ingest_input, ingest_input, reindex_input


def test_new_inputs_carry_the_flag():
    assert ingest_input("d1", 2) == '{"doc_version":2,"document_id":"d1","embed_split":true}'
    assert reindex_input(3) == '{"embed_split":true,"reindex":3}'


def test_an_old_input_without_the_flag_is_the_same_work():
    assert _same_ingest_input('{"doc_version":2,"document_id":"d1"}', ingest_input("d1", 2))
    assert _same_ingest_input('{"reindex":3}', reindex_input(3))
    assert not _same_ingest_input('{"doc_version":1,"document_id":"d1"}', ingest_input("d1", 2))
    assert not _same_ingest_input('{"reindex":2}', reindex_input(3))
    assert not _same_ingest_input(None, reindex_input(3)) and not _same_ingest_input("not json", reindex_input(3))
