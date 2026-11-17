"""W2.4: a turn's attachments become the user prompt's parts, seen by the scripted model.
Attachment bytes are read through the BlobStore (never a database row)."""

from __future__ import annotations

import asyncio

import pytest
from pydantic_ai import Agent, BinaryContent, TextContent

from eap_blob_store import FsBlobStore
from eap_run_store.chat_attachments import AttachmentMeta
from eap_worker.attachments import AttachmentGone, AttachmentTooLarge, AttachmentUnsupported, build_prompt
from eap_worker.models import scripted_echo_model

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8


class Files:
    """A BlobStore on a temp dir plus the metas of what was put in it."""

    def __init__(self, root) -> None:
        self.store = FsBlobStore(root)

    def meta(self, name: str, media_type: str, data: bytes, *, put: bool = True) -> AttachmentMeta:
        key = f"attachments/t1/id-{name}"
        if put:
            async def chunks():
                yield data

            asyncio.run(self.store.put(key, chunks(), len(data), media_type))
        return AttachmentMeta(id=f"id-{name}", name=name, media_type=media_type, size=len(data), storage_key=key)

    def prompt(self, text, metas, model_id, capabilities=None):
        return asyncio.run(build_prompt(text, metas, model_id, self.store, capabilities))


@pytest.fixture
def files(tmp_path) -> Files:
    return Files(tmp_path)


def test_no_files_is_the_plain_text(files):
    assert files.prompt("hi", [], "test:echo") == "hi"


def test_image_and_text_file_become_prompt_parts_read_through_the_store(files):
    prompt = files.prompt(
        "look", [files.meta("a.png", "image/png", PNG), files.meta("n.txt", "text/plain", b"notes")], "test:echo"
    )
    assert prompt[0] == "look"
    assert isinstance(prompt[1], BinaryContent) and prompt[1].media_type == "image/png" and prompt[1].data == PNG
    assert isinstance(prompt[2], TextContent) and "Attached file n.txt:\nnotes" == prompt[2].content


def test_a_blob_missing_from_the_store_fails_the_prompt_with_its_id(files):
    from eap_blob_store import BlobNotFound

    with pytest.raises(AttachmentGone, match="id-gone.png"):
        files.prompt("x", [files.meta("gone.png", "image/png", PNG, put=False)], "test:echo")


def test_attachment_gone_is_classified_with_a_clear_code(files):
    from eap_worker.errors import classify

    try:
        files.prompt("x", [files.meta("gone.png", "image/png", PNG, put=False)], "test:echo")
    except AttachmentGone as exc:
        failure = classify(exc)
    assert failure.code == "attachment_gone" and "no longer available" in failure.message


def test_a_blob_larger_than_its_recorded_size_is_refused(files):
    meta = files.meta("big.txt", "text/plain", b"x" * 100)
    small = AttachmentMeta(id=meta.id, name=meta.name, media_type=meta.media_type, size=10, storage_key=meta.storage_key)
    with pytest.raises(AttachmentTooLarge):
        files.prompt("x", [small], "test:echo")


def test_a_recorded_size_over_the_cap_is_refused_without_reading(files):
    meta = files.meta("huge.txt", "text/plain", b"x")
    huge = AttachmentMeta(id=meta.id, name=meta.name, media_type=meta.media_type, size=10 * 1024 * 1024 + 1,
                          storage_key=meta.storage_key)
    with pytest.raises(AttachmentTooLarge):
        files.prompt("x", [huge], "test:echo")


def test_the_scripted_model_receives_the_parts(files):
    prompt = files.prompt(
        "look", [files.meta("a.png", "image/png", PNG), files.meta("n.txt", "text/plain", b"notes")], "test:echo"
    )
    result = asyncio.run(Agent(scripted_echo_model()).run(prompt))
    assert result.output.startswith("Echo: look [file image/png 16 bytes] [text file Attached file n.txt:")
    # The stored user prompt keeps the bytes for the next turn's history.
    stored = result.new_messages()[0].parts[0].content
    assert any(isinstance(item, BinaryContent) for item in stored)


def test_a_model_that_cannot_take_images_or_pdfs_is_refused_but_text_is_inlined(files):
    with pytest.raises(AttachmentUnsupported):
        files.prompt("x", [files.meta("a.png", "image/png", PNG)], "test:ticket")
    with pytest.raises(AttachmentUnsupported):
        files.prompt("x", [files.meta("a.pdf", "application/pdf", b"%PDF-1")], "mistral:small")
    assert isinstance(files.prompt("x", [files.meta("n.md", "text/markdown", b"# t")], "test:ticket")[1], TextContent)


def test_build_prompt_uses_the_recorded_capabilities_not_the_guess(files):
    # The guess says test:ticket takes no images; the run recorded that the model does.
    recorded = {"image": True, "pdf": False, "source": "gateway"}
    prompt = files.prompt("x", [files.meta("a.png", "image/png", PNG)], "test:ticket", recorded)
    assert isinstance(prompt[1], BinaryContent)
    # The guess says a litellm model takes PDFs; the run recorded that it does not.
    with pytest.raises(AttachmentUnsupported):
        files.prompt("x", [files.meta("a.pdf", "application/pdf", b"%PDF-1")], "litellm:m", recorded)
    # Text is inlined whatever was recorded.
    none = {"image": False, "pdf": False, "source": "static"}
    assert isinstance(files.prompt("x", [files.meta("n.md", "text/markdown", b"# t")], "litellm:m", none)[1], TextContent)


def test_without_a_recorded_decision_the_old_guess_applies(files):
    with pytest.raises(AttachmentUnsupported):
        files.prompt("x", [files.meta("a.png", "image/png", PNG)], "test:ticket", None)
    assert isinstance(files.prompt("x", [files.meta("a.png", "image/png", PNG)], "test:echo", {})[1], BinaryContent)


def test_the_capabilities_come_out_of_the_runs_model_settings():
    from eap_worker.attachments import recorded_capabilities

    caps = {"image": True, "pdf": False, "source": "gateway"}
    assert recorded_capabilities({"temperature": 0.2, "attachment_capabilities": caps}) == caps
    assert recorded_capabilities({"temperature": 0.2}) is None
    assert recorded_capabilities(None) is None


def test_the_attachment_decision_is_not_a_model_setting():
    from eap_worker.workflow import apply_run_model_settings
    from eap_schemas import AgentSpec

    spec = AgentSpec(name="a", model={"provider": "test", "name": "echo"})
    out = apply_run_model_settings(spec, {"temperature": 0.5, "attachment_capabilities": {"image": True, "pdf": True, "source": "static"}})
    assert out.model_settings.temperature == 0.5
