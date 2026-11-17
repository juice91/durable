"""W2.4: a turn's attachments become the user prompt's parts, seen by the scripted model."""

from __future__ import annotations

import asyncio

import pytest
from pydantic_ai import Agent, BinaryContent, TextContent

from eap_run_store.chat_attachments import AttachmentBlob, AttachmentMeta
from eap_worker.attachments import AttachmentUnsupported, build_prompt
from eap_worker.models import scripted_echo_model

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8


def blob(name: str, media_type: str, data: bytes) -> AttachmentBlob:
    return AttachmentBlob(AttachmentMeta(id=f"id-{name}", name=name, media_type=media_type, size=len(data)), data)


def test_no_files_is_the_plain_text():
    assert build_prompt("hi", [], "test:echo") == "hi"


def test_image_and_text_file_become_prompt_parts():
    prompt = build_prompt("look", [blob("a.png", "image/png", PNG), blob("n.txt", "text/plain", b"notes")], "test:echo")
    assert prompt[0] == "look"
    assert isinstance(prompt[1], BinaryContent) and prompt[1].media_type == "image/png" and prompt[1].data == PNG
    assert isinstance(prompt[2], TextContent) and "Attached file n.txt:\nnotes" == prompt[2].content


def test_the_scripted_model_receives_the_parts():
    prompt = build_prompt("look", [blob("a.png", "image/png", PNG), blob("n.txt", "text/plain", b"notes")], "test:echo")
    result = asyncio.run(Agent(scripted_echo_model()).run(prompt))
    assert result.output.startswith("Echo: look [file image/png 16 bytes] [text file Attached file n.txt:")
    # The stored user prompt keeps the bytes for the next turn's history.
    stored = result.new_messages()[0].parts[0].content
    assert any(isinstance(item, BinaryContent) for item in stored)


def test_a_model_that_cannot_take_images_or_pdfs_is_refused_but_text_is_inlined():
    with pytest.raises(AttachmentUnsupported):
        build_prompt("x", [blob("a.png", "image/png", PNG)], "test:ticket")
    with pytest.raises(AttachmentUnsupported):
        build_prompt("x", [blob("a.pdf", "application/pdf", b"%PDF-1")], "mistral:small")
    assert isinstance(build_prompt("x", [blob("n.md", "text/markdown", b"# t")], "test:ticket")[1], TextContent)
