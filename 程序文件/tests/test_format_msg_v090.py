"""Real Outlook MSG verification using pinned, public upstream example data.

These tests never download fixtures and never open their attachments. Obtain
the fixtures explicitly for a development run, then point QINGJIAN_MSG_FIXTURES
at that directory. Missing optional external fixtures are reported as skips.
The hashes below prevent accidental testing against a user's private mail.

Source: https://github.com/TeamMsgExtractor/msg-extractor/tree/
        1f61675f39dd03ac0565147d242acfcf7d3428ba/example-msg-files
Upstream repository license: GPL-3.0. Files stay in development work evidence;
they are not bundled with the application or shipped as user examples.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import socket

import pytest

from qingjian.converter_hub import run_conversion
from qingjian.core import format_mail


FIXTURE_SHA256 = {
    "unicode.msg": "3c67dc70854b994e469fc14496d23bf778e718831806ebafc72436c840439f9c",
    "multi-to-to.msg": "1c41e77543b704f8e7c298bcab0bcac76fd20848da066d520d746d8761587eb1",
}
ATTACHMENTS = {
    "import OleFileIO.tif": (969674, "6f36cb718943751db9dc4c9df624c4390d8a13674127b7e919f419061e856dfa"),
    "raised value error.tif": (1033142, "3b43fdeca80da38c80e918e8f21cbd6fc925dac994f3922c7893fc6c1326fb92"),
}


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture
def public_msg(tmp_path, monkeypatch):
    folder = Path(os.environ.get("QINGJIAN_MSG_FIXTURES") or
                  (Path(__file__).resolve().parents[3] / "work" / "v090-msg-fixtures"))
    source = folder / "unicode.msg"
    if not source.is_file():
        pytest.skip("Optional pinned public MSG fixture absent; set QINGJIAN_MSG_FIXTURES for the MSG integration check")
    assert sha256(source) == FIXTURE_SHA256[source.name], "Fixture must be the pinned public upstream sample, not user mail"

    def no_network(*args, **kwargs):
        raise AssertionError("Reading a saved MSG must never contact a network service")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr("requests.sessions.Session.request", no_network)
    destination = tmp_path / "公开测试邮件.msg"
    shutil.copyfile(source, destination)
    return destination


def assert_attachments(folder):
    paths = {path.name: path for path in Path(folder).iterdir()}
    assert set(paths) == set(ATTACHMENTS)
    for name, (size, digest) in ATTACHMENTS.items():
        assert paths[name].stat().st_size == size
        assert sha256(paths[name]) == digest


def test_real_msg_body_headers_and_attachment_names_convert_offline(public_msg, tmp_path):
    output = tmp_path / "正文.txt"
    result = format_mail.convert(public_msg, output)
    text = output.read_text(encoding="utf-8")
    assert "主题：Test for TIF files" in text
    assert "This is a test email to experiment with the MS Outlook MSG Extractor" in text
    assert "日期：" in text and "2013-11-18" in text
    assert "\x00" not in text
    assert all(name in text for name in ATTACHMENTS)
    assert result["outputs"] == [str(output)] and result["changes"] == 1
    assert sha256(public_msg) == FIXTURE_SHA256["unicode.msg"]


def test_real_msg_attachments_preserve_exact_payload_bytes(public_msg, tmp_path):
    folder = tmp_path / "附件"
    result = format_mail.convert(public_msg, folder, {"attachments": True})
    assert result["changes"] == 2
    assert result["outputs"] == [str(folder)]
    assert_attachments(folder)
    assert sha256(public_msg) == FIXTURE_SHA256["unicode.msg"]


def test_real_msg_hub_continues_after_corrupt_input_and_never_overwrites(public_msg, tmp_path):
    corrupted = tmp_path / "损坏邮件.msg"
    corrupted.write_bytes(b"Synthetic invalid MSG fixture")
    existing = public_msg.with_suffix(".txt")
    existing.write_text("用户已有结果，不应覆盖", encoding="utf-8")
    result = run_conversion("convert:txt", [corrupted, public_msg])
    assert [row["status"] for row in result["records"]] == ["failed", "success"]
    assert len(result["errors"]) == 1 and len(result["outputs"]) == 1
    output = Path(result["outputs"][0])
    assert output.parent == public_msg.parent and output != existing
    assert "This is a test email" in output.read_text(encoding="utf-8")
    assert existing.read_text(encoding="utf-8") == "用户已有结果，不应覆盖"
    assert sha256(public_msg) == FIXTURE_SHA256["unicode.msg"]
    assert corrupted.read_bytes() == b"Synthetic invalid MSG fixture"
    assert not list(tmp_path.glob(".qj-convert-*"))


def test_real_msg_hub_extraction_uses_a_new_folder(public_msg, tmp_path):
    existing = tmp_path / (public_msg.stem + "_附件")
    existing.mkdir()
    marker = existing / "已有文件.txt"
    marker.write_text("保留", encoding="utf-8")
    result = run_conversion("convert:attachments", [public_msg])
    assert not result["errors"] and len(result["outputs"]) == 1
    output = Path(result["outputs"][0])
    assert output.is_dir() and output != existing and output.parent == tmp_path
    assert_attachments(output)
    assert marker.read_text(encoding="utf-8") == "保留"
    assert sha256(public_msg) == FIXTURE_SHA256["unicode.msg"]


def test_msg_cancellation_before_start_has_no_output(public_msg, tmp_path):
    result = run_conversion("convert:attachments", [public_msg], cancel=lambda: True)
    assert result["cancelled"] and not result["outputs"]
    assert not list(tmp_path.glob(".qj-convert-*"))
    assert sha256(public_msg) == FIXTURE_SHA256["unicode.msg"]


def test_real_msg_nul_padding_is_removed_from_txt_and_docx(public_msg, tmp_path):
    from docx import Document
    folder = Path(os.environ.get("QINGJIAN_MSG_FIXTURES") or
                  (Path(__file__).resolve().parents[3] / "work" / "v090-msg-fixtures"))
    upstream = folder / "multi-to-to.msg"
    if not upstream.is_file():
        pytest.skip("Optional pinned multi-recipient MSG boundary fixture absent")
    assert sha256(upstream) == FIXTURE_SHA256[upstream.name]
    source = tmp_path / "多收件人填充测试.msg"
    shutil.copyfile(upstream, source)
    for extension in (".txt", ".docx"):
        output = source.with_suffix(extension)
        result = format_mail.convert(source, output)
        content = output.read_text(encoding="utf-8") if extension == ".txt" else "\n".join(p.text for p in Document(output).paragraphs)
        assert "Test email body." in content and "Test: multiple To recipients" in content
        assert "\x00" not in content
        assert result["warnings"], "Control-character cleanup should be disclosed"
    assert sha256(source) == FIXTURE_SHA256["multi-to-to.msg"]
