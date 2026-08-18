"""GitStorage direkt (ohne HTTP) — OCC, Transaktions-Atomarität, Commit-Format (spec.md §5/§6/§9.1)."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from agent_md_api.domain.errors import (
    AmbiguousMatchError,
    NoMatchError,
    NotFoundError,
    ValidationError,
    VersionConflictError,
    WrongKindError,
)
from agent_md_api.domain.models import Kind, Principal, TransactionOperation, TransactionOperationType
from agent_md_api.storage.git_repo import GitStorage, classify_kind

PRINCIPAL = Principal(user_id="human:alice", client_id="web-bff")


@pytest.fixture
def storage(repo_dir: Path) -> GitStorage:
    return GitStorage(repo_dir)


def _edit_op(path: str, old: str, new: str, version: str | None) -> TransactionOperation:
    return TransactionOperation(path=path, type=TransactionOperationType.EDIT, old_str=old, new_str=new, if_version=version)


# ---- OCC / old_str-Matching (spec.md §5) -----------------------------------------------


def test_edit_success_updates_content_and_bumps_version(storage: GitStorage) -> None:
    before = storage.get_file("readme.md")
    op = _edit_op("readme.md", "Initialer Inhalt.", "Geänderter Inhalt.", before.version)

    result = storage.run_transaction([op], principal=PRINCIPAL, reason="Test-Edit", task_id=None)

    after = storage.get_file("readme.md")
    assert after.content is not None
    assert "Geänderter Inhalt." in after.content
    assert after.version != before.version
    assert result.files[0].version == after.version


def test_edit_old_str_zero_occurrences_raises_no_match(storage: GitStorage) -> None:
    version = storage.get_file("readme.md").version
    op = _edit_op("readme.md", "dieser Text kommt nirgendwo vor", "x", version)

    with pytest.raises(NoMatchError):
        storage.run_transaction([op], principal=PRINCIPAL, reason="Test", task_id=None)


def test_edit_old_str_multiple_occurrences_raises_ambiguous_match(storage: GitStorage) -> None:
    # Zwei Vorkommen von "a" im Startinhalt "# Testbaum\nInitialer Inhalt.\n" erzeugen.
    version = storage.get_file("readme.md").version
    setup = _edit_op("readme.md", "Initialer Inhalt.", "aaa bbb aaa", version)
    storage.run_transaction([setup], principal=PRINCIPAL, reason="Setup", task_id=None)

    version2 = storage.get_file("readme.md").version
    op = _edit_op("readme.md", "aaa", "ccc", version2)

    with pytest.raises(AmbiguousMatchError):
        storage.run_transaction([op], principal=PRINCIPAL, reason="Test", task_id=None)


def test_edit_version_conflict_returns_current_content_and_version(storage: GitStorage) -> None:
    op = _edit_op("readme.md", "Initialer Inhalt.", "x", "veraltete-version-die-nicht-existiert")

    with pytest.raises(VersionConflictError) as exc_info:
        storage.run_transaction([op], principal=PRINCIPAL, reason="Test", task_id=None)

    err = exc_info.value
    assert err.current_content is not None
    assert "Initialer Inhalt." in err.current_content
    assert err.current_version == storage.get_file("readme.md").version


def test_delete_without_if_version_raises_validation_error(storage: GitStorage) -> None:
    op = TransactionOperation(path="readme.md", type=TransactionOperationType.DELETE, if_version=None)

    with pytest.raises(ValidationError):
        storage.run_transaction([op], principal=PRINCIPAL, reason="Test", task_id=None)


def test_append_to_nonexistent_file_raises_not_found(storage: GitStorage) -> None:
    op = TransactionOperation(path="nicht-vorhanden.md", type=TransactionOperationType.APPEND, content="x", if_version="egal")

    with pytest.raises(NotFoundError):
        storage.run_transaction([op], principal=PRINCIPAL, reason="Test", task_id=None)


def test_full_write_new_file_does_not_require_if_version(storage: GitStorage) -> None:
    op = TransactionOperation(path="neu.md", type=TransactionOperationType.WRITE, content="Neuer Inhalt", if_version=None)

    storage.run_transaction([op], principal=PRINCIPAL, reason="Neue Datei", task_id=None)

    assert storage.get_file("neu.md").content == "Neuer Inhalt"


def test_full_write_existing_file_requires_if_version(storage: GitStorage) -> None:
    op = TransactionOperation(path="readme.md", type=TransactionOperationType.WRITE, content="Überschrieben", if_version=None)

    with pytest.raises(ValidationError):
        storage.run_transaction([op], principal=PRINCIPAL, reason="Test", task_id=None)


def test_write_binary_and_delete_use_bytes_hash_as_version(storage: GitStorage) -> None:
    result = storage.write_binary(
        "bild.png", b"\x89PNG-fake-bytes", if_version=None, principal=PRINCIPAL, reason="Upload", task_id=None
    )
    assert result.files[0].version == storage.blob_version("bild.png")

    with pytest.raises(WrongKindError):
        # Text-Kind darf nicht über den Binär-Upload-Pfad geschrieben werden.
        storage.write_binary("readme.md", b"x", if_version="egal", principal=PRINCIPAL, reason="x", task_id=None)


# ---- Transaktionen: Atomarität (spec.md §6) --------------------------------------------


def test_transaction_move_write_and_delete_in_single_commit(storage: GitStorage) -> None:
    """Move-Fall: neue Datei schreiben + alte löschen, EIN Commit für beide (spec.md §14.3)."""
    old_version = storage.get_file("readme.md").version
    ops = [
        TransactionOperation(path="neu/readme.md", type=TransactionOperationType.WRITE, content="verschoben", if_version=None),
        TransactionOperation(path="readme.md", type=TransactionOperationType.DELETE, if_version=old_version),
    ]

    result = storage.run_transaction(ops, principal=PRINCIPAL, reason="Move", task_id=None)

    with pytest.raises(NotFoundError):
        storage.get_file("readme.md")
    assert storage.get_file("neu/readme.md").content == "verschoben"
    # Ein Commit für beide Operationen — result.files enthält nur die überlebende Datei
    # (die gelöschte ist bewusst ausgeschlossen, siehe GitStorage.run_transaction).
    assert [f.path for f in result.files] == ["neu/readme.md"]
    assert result.commit_id == storage._repo.head.commit.hexsha  # noqa: SLF001 - Whitebox-Check des einen Commits


def test_transaction_failure_writes_nothing(storage: GitStorage) -> None:
    """Phase 1 validiert ALLE Operationen, bevor Phase 2 überhaupt schreibt (spec.md §6) —
    schlägt eine Operation fehl, darf NICHTS geschrieben worden sein, auch nicht die
    an sich gültigen Operationen davor in der Liste."""
    head_before = storage._repo.head.commit.hexsha  # noqa: SLF001
    version = storage.get_file("readme.md").version
    ops = [
        TransactionOperation(path="readme.md", type=TransactionOperationType.WRITE, content="sollte nie ankommen", if_version=version),
        TransactionOperation(path="data.json", type=TransactionOperationType.DELETE, if_version="falsche-version"),
    ]

    with pytest.raises(VersionConflictError):
        storage.run_transaction(ops, principal=PRINCIPAL, reason="Test", task_id=None)

    assert storage.get_file("readme.md").content == "# Testbaum\nInitialer Inhalt.\n"
    assert storage.get_file("data.json").content == '{"key": "value"}'
    assert storage._repo.head.commit.hexsha == head_before  # noqa: SLF001 - kein neuer Commit entstanden


def test_transaction_without_operations_raises_validation_error(storage: GitStorage) -> None:
    with pytest.raises(ValidationError):
        storage.run_transaction([], principal=PRINCIPAL, reason="leer", task_id=None)


# ---- Commit-Message-Format (spec.md §9.1) ------------------------------------------------


def test_commit_message_includes_user_client_task_and_reason(storage: GitStorage) -> None:
    version = storage.get_file("readme.md").version
    op = _edit_op("readme.md", "Initialer Inhalt.", "x", version)
    principal = Principal(user_id="human:alice", client_id="web-bff")

    storage.run_transaction([op], principal=principal, reason="Preis aktualisiert", task_id="lv-42")

    header = storage._repo.head.commit.message.splitlines()[0]  # noqa: SLF001
    assert header == "[user:human:alice] [client:web-bff] [task:lv-42] Preis aktualisiert"


def test_commit_message_omits_task_marker_when_no_task_id(storage: GitStorage) -> None:
    version = storage.get_file("readme.md").version
    op = _edit_op("readme.md", "Initialer Inhalt.", "x", version)

    storage.run_transaction([op], principal=PRINCIPAL, reason="Ohne Task", task_id=None)

    header = storage._repo.head.commit.message.splitlines()[0]  # noqa: SLF001
    assert header == "[user:human:alice] [client:web-bff] Ohne Task"
    assert "[task:" not in header


# ---- classify_kind (spec.md §3) --------------------------------------------------------


def test_classify_kind_skill() -> None:
    assert classify_kind("grundbuch.skill") == Kind.SKILL


def test_classify_kind_skill_is_a_text_kind() -> None:
    """`.skill`-Dateien sind wie `.md`/`.json` per str_replace/append/Full-Write editierbar,
    nicht wie `binary` nur per Full-Replace (spec.md §3/§5)."""
    from agent_md_api.domain.models import TEXT_KINDS

    assert Kind.SKILL in TEXT_KINDS


def test_classify_kind_still_distinguishes_json_md_and_pii_json() -> None:
    """Regression: `.skill` darf die bestehende Reihenfolge/Zuordnung nicht verändern."""
    assert classify_kind("vertrag.json") == Kind.JSON
    assert classify_kind("vertrag.pii.json") == Kind.PII_JSON
    assert classify_kind("notiz.md") == Kind.MD
    assert classify_kind("scan.pdf") == Kind.BINARY


def test_skill_file_roundtrip_via_storage(storage: GitStorage) -> None:
    """Eine Dateifamilie: `.skill` liegt neben `.json`/`.md` mit demselben Basisnamen und
    verhält sich wie jede andere Text-Datei -- volle Schreib-/Edit-Semantik, eigene Version."""
    write_op = TransactionOperation(
        path="grundbuch.skill",
        type=TransactionOperationType.WRITE,
        content="1. Öffne die PDF.\n2. Suche nach 'Eigentümer:'.\n3. Trage den Namen in grundbuch.json ein.",
        if_version=None,
    )
    storage.run_transaction([write_op], principal=PRINCIPAL, reason="Skill anlegen", task_id=None)

    file = storage.get_file("grundbuch.skill")
    assert file.kind == Kind.SKILL
    assert "Eigentümer" in (file.content or "")

    version = file.version
    edit_op = _edit_op("grundbuch.skill", "1. Öffne die PDF.", "1. Öffne das gescannte Dokument.", version)
    storage.run_transaction([edit_op], principal=PRINCIPAL, reason="Skill präzisieren", task_id=None)

    updated = storage.get_file("grundbuch.skill")
    assert "gescannte Dokument" in (updated.content or "")
    assert updated.version != version


# ---- Thread-Sicherheit bei parallelen Reads (Produktions-Bug: gitdb-Race nach Neustart) -----


def test_concurrent_reads_are_serialized_through_the_lock(storage: GitStorage) -> None:
    """Regression: `git.Repo`/gitdb halten intern einen lazy befüllten Tree-/Blob-Cache
    (`gitdb.util.LazyMixin`), der nicht thread-sicher ist. FastAPI führt synchrone Endpunkte/
    Dependencies in einem Thread-Pool aus (spec.md §6-Docstring in git_repo.py) — mehrere
    gleichzeitige `GET /file`-Requests liefen bisher ungebremst parallel durch `_blob_version`
    und konnten in Produktion, besonders direkt nach einem Neustart mit noch leerem Cache,
    denselben Cache-Slot gleichzeitig befüllen. Beobachtet als `IndexError`/`binascii.Error`
    mitten in gitdb, nicht als echte Repo-Korruption (`git fsck` blieb sauber).

    Statt auf den seltenen, timing-abhängigen Crash zu warten: direkt nachweisen, dass nie
    zwei Threads gleichzeitig in `_blob_version` sind, indem die Methode instrumentiert und
    das Zeitfenster künstlich vergrößert wird (`time.sleep`) -- ohne den Fix in `get_file`
    (Lock um den gesamten Lesezugriff) würde `max_concurrent` hier > 1 werden."""
    concurrent_calls = 0
    max_concurrent = 0
    guard = threading.Lock()
    original_blob_version = storage._blob_version  # noqa: SLF001

    def instrumented_blob_version(path: str) -> str | None:
        nonlocal concurrent_calls, max_concurrent
        with guard:
            concurrent_calls += 1
            max_concurrent = max(max_concurrent, concurrent_calls)
        try:
            time.sleep(0.01)  # Zeitfenster künstlich vergrößern, damit ein Race sicher auffällt
            return original_blob_version(path)
        finally:
            with guard:
                concurrent_calls -= 1

    storage._blob_version = instrumented_blob_version  # type: ignore[method-assign]  # noqa: SLF001

    errors: list[Exception] = []

    def worker() -> None:
        try:
            storage.get_file("readme.md")
        except Exception as exc:  # noqa: BLE001 - jede Exception hier wäre der reproduzierte Bug
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Concurrent get_file() raised: {errors}"
    assert max_concurrent == 1, "Zwei Threads waren gleichzeitig in _blob_version -- Lock schützt Reads nicht."
