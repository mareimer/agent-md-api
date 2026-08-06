"""Tests für `webdav_bridge.agent_api_client.AgentApiClient` (spec.md §14.2/§14.3) — GEGEN
EINE ECHTE, LAUFENDE Agent-API-Instanz (Fixture `agent_api_client`, siehe conftest.py),
kein Mock. Das ist bewusst so, weil genau die HTTP-Übersetzung (Header, Fehler-Mapping,
Transaktionsform bei move/copy) der zu testende Kern dieses Moduls ist."""

from __future__ import annotations

import pytest

from tests.conftest import DUMMY_PDF_BYTES
from webdav_bridge.agent_api_client import (
    AgentApiClient,
    AgentApiConflictError,
    AgentApiNotFoundError,
)


def test_list_tree_root_lists_direct_children_only(agent_api_client: AgentApiClient) -> None:
    entries = agent_api_client.list_tree(user_id="human:alice-test", root="", depth=1)
    paths = {e.path for e in entries}

    assert paths == {"readme.md", "data.json", "person.pii.json", "dummy.pdf", "sub"}
    assert "sub/nested.md" not in paths  # Enkelkind, nicht Teil von depth=1 (spec.md §4)

    sub_entry = next(e for e in entries if e.path == "sub")
    assert sub_entry.kind == "dir"


def test_list_tree_deeper_root_lists_nested_file(agent_api_client: AgentApiClient) -> None:
    entries = agent_api_client.list_tree(user_id="human:alice-test", root="sub", depth=1)
    paths = {e.path for e in entries}

    assert paths == {"sub/nested.md"}


def test_get_file_returns_text_content_and_version(agent_api_client: AgentApiClient) -> None:
    meta = agent_api_client.get_file(user_id="human:alice-test", path="readme.md")

    assert meta.kind == "md"
    assert meta.content == "# Testbaum\nInitialer Inhalt.\n"
    assert meta.version  # Git-Blob-SHA, nicht leer


def test_get_file_unknown_path_raises_not_found(agent_api_client: AgentApiClient) -> None:
    with pytest.raises(AgentApiNotFoundError):
        agent_api_client.get_file(user_id="human:alice-test", path="nie-existiert.md")


def test_get_file_content_returns_raw_binary_bytes(agent_api_client: AgentApiClient) -> None:
    data = agent_api_client.get_file_content(user_id="human:alice-test", path="dummy.pdf")

    assert data == DUMMY_PDF_BYTES


def test_write_text_creates_new_file(agent_api_client: AgentApiClient) -> None:
    version = agent_api_client.write_text(
        user_id="human:alice-test", path="notes.md", content="Neu erstellt.", if_version=None, reason="Test: neue Datei"
    )

    assert version
    meta = agent_api_client.get_file(user_id="human:alice-test", path="notes.md")
    assert meta.content == "Neu erstellt."
    assert meta.version == version


def test_write_text_updates_existing_file_with_correct_if_version(agent_api_client: AgentApiClient) -> None:
    current = agent_api_client.get_file(user_id="human:alice-test", path="readme.md")

    new_version = agent_api_client.write_text(
        user_id="human:alice-test",
        path="readme.md",
        content="# Testbaum\nAktualisiert.\n",
        if_version=current.version,
        reason="Test: Update",
    )

    assert new_version != current.version
    updated = agent_api_client.get_file(user_id="human:alice-test", path="readme.md")
    assert updated.content == "# Testbaum\nAktualisiert.\n"


def test_write_text_stale_if_version_raises_conflict_with_current_state(agent_api_client: AgentApiClient) -> None:
    current = agent_api_client.get_file(user_id="human:alice-test", path="readme.md")
    # Datei unabhängig ändern, damit `current.version` jetzt veraltet ist.
    agent_api_client.write_text(
        user_id="human:alice-test", path="readme.md", content="Von anderswo geändert.", if_version=current.version, reason="Race"
    )

    with pytest.raises(AgentApiConflictError) as exc_info:
        agent_api_client.write_text(
            user_id="human:alice-test",
            path="readme.md",
            content="Mein veralteter Schreibversuch.",
            if_version=current.version,
            reason="Test: veraltete Version",
        )

    assert exc_info.value.current_content == "Von anderswo geändert."
    assert exc_info.value.current_version
    assert exc_info.value.current_version != current.version


def test_write_binary_uploads_new_file(agent_api_client: AgentApiClient) -> None:
    payload = b"\x89PNG\r\n\x1a\nfake-png-bytes"
    version = agent_api_client.write_binary(
        user_id="human:alice-test", path="images/logo.png", data=payload, if_version=None, reason="Test: Binär-Upload"
    )

    assert version
    data = agent_api_client.get_file_content(user_id="human:alice-test", path="images/logo.png")
    assert data == payload


def test_delete_removes_file(agent_api_client: AgentApiClient) -> None:
    current = agent_api_client.get_file(user_id="human:alice-test", path="data.json")

    agent_api_client.delete(user_id="human:alice-test", path="data.json", if_version=current.version, reason="Test: Löschen")

    with pytest.raises(AgentApiNotFoundError):
        agent_api_client.get_file(user_id="human:alice-test", path="data.json")


def test_delete_with_stale_if_version_raises_conflict(agent_api_client: AgentApiClient) -> None:
    current = agent_api_client.get_file(user_id="human:alice-test", path="data.json")
    agent_api_client.write_text(
        user_id="human:alice-test", path="data.json", content='{"changed": true}', if_version=current.version, reason="Race"
    )

    with pytest.raises(AgentApiConflictError):
        agent_api_client.delete(user_id="human:alice-test", path="data.json", if_version=current.version, reason="Test: veraltet")


def test_move_text_relocates_content_in_one_commit(agent_api_client: AgentApiClient, agent_api) -> None:
    current = agent_api_client.get_file(user_id="human:alice-test", path="readme.md")
    commits_before = agent_api.commit_count()

    agent_api_client.move_text(
        user_id="human:alice-test",
        src_path="readme.md",
        dest_path="moved/readme.md",
        content=current.content or "",
        src_version=current.version,
        reason="Test: Move",
    )

    assert agent_api.commit_count() == commits_before + 1  # atomare Transaktion, spec.md §14.3

    dest = agent_api_client.get_file(user_id="human:alice-test", path="moved/readme.md")
    assert dest.content == current.content

    with pytest.raises(AgentApiNotFoundError):
        agent_api_client.get_file(user_id="human:alice-test", path="readme.md")


def test_copy_text_leaves_source_untouched(agent_api_client: AgentApiClient, agent_api) -> None:
    current = agent_api_client.get_file(user_id="human:alice-test", path="readme.md")
    commits_before = agent_api.commit_count()

    agent_api_client.copy_text(
        user_id="human:alice-test", dest_path="copy-of-readme.md", content=current.content or "", reason="Test: Copy"
    )

    assert agent_api.commit_count() == commits_before + 1

    dest = agent_api_client.get_file(user_id="human:alice-test", path="copy-of-readme.md")
    assert dest.content == current.content
    # Quelle unverändert vorhanden
    src = agent_api_client.get_file(user_id="human:alice-test", path="readme.md")
    assert src.content == current.content


def test_token_is_cached_across_calls(agent_api_client: AgentApiClient) -> None:
    """spec.md §14.2: kurzlebiges User-Token wird gemintet und bis kurz vor Ablauf gecacht,
    um nicht bei jedem einzelnen WebDAV-Request (PROPFIND-Stürme) neu zu signieren. Prüft
    das Cache-Verhalten direkt über den internen `_TokenCache` (Whitebox — es gibt keine
    öffentliche API, die den JWT-Wert exponiert, um es rein über Verhalten zu beobachten)."""
    agent_api_client.get_file(user_id="human:alice-test", path="readme.md")
    first_token = agent_api_client._tokens.get("human:alice-test")
    assert first_token is not None

    agent_api_client.get_file(user_id="human:alice-test", path="readme.md")
    second_token = agent_api_client._tokens.get("human:alice-test")

    assert first_token[0] == second_token[0]  # identisches JWT, kein Neu-Minten
