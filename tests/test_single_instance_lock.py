"""Ein-Instanz-Garantie (spec.md §6) — `main._acquire_single_instance_lock`.

Zwei Worker-Prozesse (oder zwei Replicas) dürfen NIE gleichzeitig gegen denselben
Git-Arbeitsbaum schreiben — ein In-Process-Lock allein würde das nicht verhindern,
deshalb ein Datei-Lock (filelock), der beim Start hart geprüft wird. Ein zweiter
Versuch mit demselben Lock-Pfad muss beim Start sofort scheitern (kein Warten, `timeout=0`)."""

from __future__ import annotations

from pathlib import Path

import pytest
from filelock import FileLock, Timeout

from agent_md_api import main as main_module
from agent_md_api.config import Settings


def _settings_for(tmp_path: Path, repo_dir: Path) -> Settings:
    return Settings(
        root_dir=repo_dir,
        audit_backend="none",
        client_registry_path=tmp_path / "clients.json",
        instance_lock_path=tmp_path / "instance.lock",
    )


def test_second_acquisition_attempt_fails_with_system_exit(tmp_path: Path, repo_dir: Path) -> None:
    settings = _settings_for(tmp_path, repo_dir)

    lock1 = main_module._acquire_single_instance_lock(settings)  # noqa: SLF001 - gezielter Whitebox-Test
    try:
        with pytest.raises(SystemExit):
            main_module._acquire_single_instance_lock(settings)  # noqa: SLF001
    finally:
        lock1.release()


def test_lock_can_be_reacquired_after_release(tmp_path: Path, repo_dir: Path) -> None:
    """Nach ordnungsgemäßem `release()` (z.B. beim Lifespan-Shutdown) muss ein neuer
    Start wieder funktionieren — die Sperre ist prozesslebensdauer-gebunden, kein
    dauerhafter Zustand."""
    settings = _settings_for(tmp_path, repo_dir)

    lock1 = main_module._acquire_single_instance_lock(settings)  # noqa: SLF001
    lock1.release()

    lock2 = main_module._acquire_single_instance_lock(settings)  # noqa: SLF001
    lock2.release()


def test_raw_filelock_timeout_is_the_underlying_mechanism(tmp_path: Path) -> None:
    """Belegt den Baustein UNTER `_acquire_single_instance_lock` direkt: `filelock.Timeout`
    ist die Exception, die die Funktion abfängt und in `SystemExit` übersetzt."""
    lock_path = tmp_path / "raw.lock"
    lock1 = FileLock(str(lock_path))
    lock1.acquire(timeout=0)
    try:
        lock2 = FileLock(str(lock_path))
        with pytest.raises(Timeout):
            lock2.acquire(timeout=0)
    finally:
        lock1.release()


def test_full_app_startup_fails_when_lock_already_held(
    tmp_path: Path, repo_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """End-to-End-Variante über den echten Lifespan: eine zweite `TestClient(app)`-Instanz
    gegen denselben `AGENT_API_INSTANCE_LOCK_PATH` darf nicht hochfahren.

    Auf diesem Stack (anyio-Blocking-Portal-Thread für die Lifespan) landet das
    `SystemExit`, das `_acquire_single_instance_lock` wirft, NICHT 1:1 beim Aufrufer —
    je nach anyio-Version taucht es als `SystemExit`, in eine `BaseExceptionGroup`
    verpackt, oder (beobachtet auf dieser Kombination) nur noch indirekt als
    `CancelledError` beim wartenden Future auf, während das eigentliche `SystemExit`
    asynchron im Log landet. Der Exception-*Typ* ist hier bewusst NICHT das Kriterium
    (zu implementierungsabhängig) — stattdessen wird verifiziert, was stabil bleibt:
    (a) der zweite Start scheitert überhaupt (irgendeine Exception), UND (b) die
    FATAL-Meldung aus `_acquire_single_instance_lock` erscheint auf stderr."""
    from conftest import configure_env

    configure_env(monkeypatch, tmp_path, repo_dir)

    from fastapi.testclient import TestClient

    from agent_md_api.main import app

    with TestClient(app):
        raised: BaseException | None = None
        try:
            with TestClient(app):
                pass
        except BaseException as exc:  # noqa: BLE001 - Wrapping-Tiefe ist anyio-Implementierungsdetail
            raised = exc

        assert raised is not None, "zweiter Start hätte fehlschlagen müssen"
        stderr = capsys.readouterr().err
        assert "bereits gesperrt" in stderr
