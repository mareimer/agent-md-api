"""Git-Arbeitsbaum als Storage-Backend (spec.md §3/§6).

Locking-Modell (spec.md §6, sicherheitskritisch — kein Implementierungsdetail):
Ein `threading.Lock` umschließt Phase 1 (Validierung) + Phase 2 (Datei-Writes +
Commit) einer Transaktion vollständig. `threading.Lock` statt `asyncio.Lock`,
weil GitPython/Dateisystem-I/O blockierend ist und FastAPI synchrone Handler
in einem Thread-Pool ausführt — die Sperre muss also über Threads hinweg
gelten, nicht nur über Coroutinen innerhalb eines Event-Loops.

Voraussetzung: genau ein Worker-Prozess pro Instanz (main.py prüft das beim
Start hart). Innerhalb dieses einen Prozesses schützt der Lock zuverlässig
vor Race Conditions zwischen Threads.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path

import git
from git.exc import InvalidGitRepositoryError, NoSuchPathError

from agent_md_api.domain.errors import (
    AmbiguousMatchError,
    NoMatchError,
    NotFoundError,
    ValidationError,
    VersionConflictError,
    WrongKindError,
)
from agent_md_api.domain.models import (
    TEXT_KINDS,
    FileResponse,
    Kind,
    Principal,
    TransactionOperation,
    TransactionOperationType,
    TransactionResult,
    TransactionResultFile,
    TreeEntry,
    TreeResponse,
)


def classify_kind(path: str) -> Kind:
    """Kind-Zuordnung rein aus dem Dateinamen (spec.md §3) — `.pii.json` vor `.json` prüfen."""
    if path.endswith(".pii.json"):
        return Kind.PII_JSON
    if path.endswith(".json"):
        return Kind.JSON
    if path.endswith(".md"):
        return Kind.MD
    return Kind.BINARY


_MIME_BY_SUFFIX = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


def guess_mime_type(path: str) -> str:
    suffix = Path(path).suffix.lower()
    return _MIME_BY_SUFFIX.get(suffix, "application/octet-stream")


class GitStorage:
    """Einzige Schreibautorität für den konfigurierten Git-Arbeitsbaum (spec.md §2)."""

    def __init__(self, root_dir: Path) -> None:
        try:
            self._repo = git.Repo(root_dir)
        except (InvalidGitRepositoryError, NoSuchPathError) as exc:
            raise RuntimeError(
                f"AGENT_API_ROOT_DIR={root_dir} ist kein bestehender Git-Arbeitsbaum. "
                "Die Agent-API initialisiert kein Repo automatisch (deployment.md §2/§6) — "
                "Migration/`git init` sind ein eigener, vorgelagerter Schritt."
            ) from exc
        self._root = Path(self._repo.working_tree_dir)  # type: ignore[arg-type]
        self._lock = threading.Lock()

    # ---- Read-Pfad (spec.md §4) -------------------------------------------------

    def _blob_version(self, path: str) -> str | None:
        """Git-Blob-SHA der Datei im zuletzt committeten Stand, None wenn nicht vorhanden."""
        if not self._repo.head.is_valid():
            return None
        try:
            blob = self._repo.head.commit.tree / path
        except KeyError:
            return None
        return blob.hexsha

    def _exists_in_tree(self, path: str) -> bool:
        return self._blob_version(path) is not None

    def list_tree(self, root: str, depth: int, cursor: str | None) -> TreeResponse:
        root = root.strip("/")
        base = self._root / root if root else self._root
        if not base.exists():
            raise NotFoundError(f"Pfad nicht gefunden: /{root}")

        entries: list[TreeEntry] = []
        if depth == 0:
            # "nur Metadaten von root" (spec.md §4) — root selbst als einzelner Eintrag,
            # keine Kinder. Für die Baumwurzel ("") gibt es keine sinnvolle Selbst-
            # Metadaten-Darstellung (kein Pfad, kein Blob) -> leere Liste.
            if root:
                entries.append(self._entry_for(base, root))
        else:
            self._walk(base, root, depth, entries)
        entries.sort(key=lambda e: e.path)

        # Einfache alphabetische Cursor-Pagination (spec.md §4) — Feinschliff (Seitengröße
        # konfigurierbar, stabile Sortierung bei Umbenennungen) ist bewusst spätere Iteration.
        page_size = 200
        start = 0
        if cursor:
            for i, e in enumerate(entries):
                if e.path > cursor:
                    start = i
                    break
            else:
                start = len(entries)
        page = entries[start : start + page_size]
        next_cursor = page[-1].path if len(entries) > start + page_size else None
        return TreeResponse(entries=page, next_cursor=next_cursor)

    def _entry_for(self, full: Path, rel_path: str) -> TreeEntry:
        """Ein einzelner TreeEntry für genau `full`/`rel_path` selbst (nicht seine Kinder) —
        für depth=0 (spec.md §4: "nur Metadaten von root")."""
        if full.is_dir():
            return TreeEntry(path=rel_path, kind=Kind.DIR)
        kind = classify_kind(full.name)
        version = self._blob_version(rel_path)
        if kind in TEXT_KINDS:
            # Größe aus dem gelesenen Inhalt berechnen, NICHT stat().st_size: Python übersetzt
            # beim Text-Read universelle Zeilenenden (\r\n -> \n), stat() sieht die Rohbytes auf
            # der Platte. Bei CRLF-Dateien würden size und len(content) sonst auseinanderlaufen —
            # das bringt einem WebDAV-Client (Content-Length passt nicht zum Body) die Verbindung
            # zum Hängen. size muss immer zu dem passen, was tatsächlich ausgeliefert wird.
            content = full.read_text(encoding="utf-8", errors="replace")
            return TreeEntry(path=rel_path, kind=kind, size=len(content.encode("utf-8")), version=version, preview=content[:200])
        return TreeEntry(path=rel_path, kind=kind, size=full.stat().st_size, version=version, mime_type=guess_mime_type(full.name))

    def _walk(self, base: Path, rel_root: str, depth: int, out: list[TreeEntry]) -> None:
        """`depth` = wie viele Ebenen unterhalb von `base` noch aufgelistet werden.
        depth=1 -> nur direkte Kinder von `base` (keine Rekursion mehr); depth=2 ->
        Kinder + deren Kinder; usw. (spec.md §4, `_entry_for` deckt depth=0 gesondert ab)."""
        if base.name == ".git":
            return
        for child in sorted(base.iterdir()):
            if child.name == ".git":
                continue
            rel_path = f"{rel_root}/{child.name}" if rel_root else child.name
            if child.is_dir():
                out.append(TreeEntry(path=rel_path, kind=Kind.DIR))
                if depth > 1:
                    self._walk(child, rel_path, depth - 1, out)
            else:
                out.append(self._entry_for(child, rel_path))

    def get_file(self, path: str) -> FileResponse:
        full = self._resolve(path)
        if not full.exists() or full.is_dir():
            raise NotFoundError(f"Datei nicht gefunden: /{path}")
        kind = classify_kind(path)
        version = self._blob_version(path)
        assert version is not None  # Datei existiert im Arbeitsbaum -> muss committet sein (Regelfall)
        if kind in TEXT_KINDS:
            # size aus dem Inhalt berechnet, nicht stat().st_size — Begründung in _entry_for().
            content = full.read_text(encoding="utf-8")
            return FileResponse(path=path, kind=kind, version=version, size=len(content.encode("utf-8")), content=content)
        return FileResponse(path=path, kind=kind, version=version, size=full.stat().st_size, mime_type=guess_mime_type(path))

    def get_file_content_bytes(self, path: str) -> tuple[bytes, str]:
        full = self._resolve(path)
        if not full.exists() or full.is_dir():
            raise NotFoundError(f"Datei nicht gefunden: /{path}")
        if classify_kind(path) in TEXT_KINDS:
            raise WrongKindError(f"/{path} ist kein `binary` — GET /file/{{path}} statt .../content nutzen.")
        return full.read_bytes(), guess_mime_type(path)

    def _resolve(self, path: str) -> Path:
        full = (self._root / path.strip("/")).resolve()
        if self._root.resolve() not in full.parents and full != self._root.resolve():
            raise ValidationError(f"Ungültiger Pfad: /{path}")
        return full

    # ---- Write-Pfad (spec.md §5/§6) ---------------------------------------------

    def run_transaction(
        self,
        operations: list[TransactionOperation],
        *,
        principal: Principal,
        reason: str,
        task_id: str | None,
    ) -> TransactionResult:
        if not operations:
            raise ValidationError("Transaktion ohne Operationen.")

        with self._lock:
            # Phase 1: alle Operationen validieren, nichts schreiben (spec.md §6).
            plan = [self._validate_operation(op) for op in operations]

            # Phase 2: erst wenn alle valide sind, schreiben + EIN Commit.
            for op, _current in zip(operations, plan, strict=True):
                self._apply_operation(op)

            paths = [op.path for op in operations]
            commit_id = self._commit(principal=principal, reason=reason, task_id=task_id, files=paths)

            deleted_paths = {op.path for op in operations if op.type is TransactionOperationType.DELETE}
            result_files = [
                TransactionResultFile(path=p, version=self._blob_version(p) or "")
                for p in dict.fromkeys(paths)  # Reihenfolge erhalten, Duplikate raus
                if p not in deleted_paths
            ]
            return TransactionResult(commit_id=commit_id, files=result_files)

    def _validate_operation(self, op: TransactionOperation) -> str | None:
        """Prüft Version/String-Match, gibt aktuellen Inhalt zurück (für Fehlermeldungen)."""
        full = self._resolve(op.path)
        exists = full.exists() and not full.is_dir()
        current_version = self._blob_version(op.path) if exists else None
        current_content = full.read_text(encoding="utf-8") if exists and classify_kind(op.path) in TEXT_KINDS else None

        if op.type is TransactionOperationType.WRITE:
            if classify_kind(op.path) not in TEXT_KINDS:
                raise ValidationError(
                    f"/{op.path}: Transaktionen unterstützen bisher nur Text-Kinds für `write` "
                    "(spec.md §14.3) — Binärdateien über POST /file/{path} (multipart)."
                )
            if op.content is None:
                raise ValidationError(f"/{op.path}: `content` fehlt bei type=write.")
            self._check_version(op, exists, current_version, current_content, require_if_version=exists)
            return current_content

        if not exists:
            raise NotFoundError(f"/{op.path}: nicht gefunden (type={op.type}).")
        if classify_kind(op.path) not in TEXT_KINDS and op.type is not TransactionOperationType.DELETE:
            raise WrongKindError(f"/{op.path}: `{op.type}` gilt nur für Text-Kinds, nicht binary.")

        self._check_version(op, exists, current_version, current_content, require_if_version=True)

        if op.type is TransactionOperationType.EDIT:
            if op.old_str is None or op.new_str is None:
                raise ValidationError(f"/{op.path}: `old_str`/`new_str` fehlen bei type=edit.")
            assert current_content is not None
            occurrences = current_content.count(op.old_str)
            if occurrences == 0:
                raise NoMatchError(f"/{op.path}: old_str kommt nicht im aktuellen Inhalt vor.")
            if occurrences > 1:
                raise AmbiguousMatchError(f"/{op.path}: old_str kommt {occurrences}x vor, nicht eindeutig.")
        elif op.type is TransactionOperationType.APPEND and op.content is None:
            raise ValidationError(f"/{op.path}: `content` fehlt bei type=append.")

        return current_content

    def _check_version(
        self,
        op: TransactionOperation,
        exists: bool,
        current_version: str | None,
        current_content: str | None,
        *,
        require_if_version: bool,
    ) -> None:
        if not exists:
            if op.if_version is not None:
                raise VersionConflictError(
                    f"/{op.path}: existiert nicht, if_version kann nicht matchen.",
                    current_content=None,
                    current_version="",
                )
            return
        if require_if_version and op.if_version is None:
            raise ValidationError(f"/{op.path}: `if_version` fehlt (Datei existiert bereits).")
        if op.if_version is not None and op.if_version != current_version:
            raise VersionConflictError(
                f"/{op.path}: Version veraltet.",
                current_content=current_content,
                current_version=current_version or "",
            )

    def _apply_operation(self, op: TransactionOperation) -> None:
        full = self._resolve(op.path)
        if op.type is TransactionOperationType.DELETE:
            full.unlink()
            self._repo.index.remove([op.path])
            return

        full.parent.mkdir(parents=True, exist_ok=True)
        if op.type is TransactionOperationType.WRITE:
            full.write_text(op.content or "", encoding="utf-8")
        elif op.type is TransactionOperationType.APPEND:
            with full.open("a", encoding="utf-8") as f:
                f.write(op.content or "")
        elif op.type is TransactionOperationType.EDIT:
            current = full.read_text(encoding="utf-8")
            full.write_text(current.replace(op.old_str or "", op.new_str or "", 1), encoding="utf-8")
        self._repo.index.add([op.path])

    def write_binary(
        self, path: str, data: bytes, *, if_version: str | None, principal: Principal, reason: str, task_id: str | None
    ) -> TransactionResult:
        """Full Replace/Upload für `kind: binary` (spec.md §5) — eigener Pfad, da Bytes statt JSON-`content`."""
        if classify_kind(path) in TEXT_KINDS:
            raise WrongKindError(f"/{path}: Text-Kind, nicht über den Binär-Upload-Pfad schreiben.")
        with self._lock:
            full = self._resolve(path)
            exists = full.exists() and not full.is_dir()
            current_version = self._blob_version(path) if exists else None
            self._check_version(
                TransactionOperation(path=path, type=TransactionOperationType.WRITE, if_version=if_version),
                exists,
                current_version,
                None,
                require_if_version=exists,
            )
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_bytes(data)
            self._repo.index.add([path])
            commit_id = self._commit(principal=principal, reason=reason, task_id=task_id, files=[path])
            return TransactionResult(
                commit_id=commit_id, files=[TransactionResultFile(path=path, version=self._blob_version(path) or "")]
            )

    def delete(
        self, path: str, *, if_version: str, principal: Principal, reason: str, task_id: str | None
    ) -> TransactionResult:
        op = TransactionOperation(path=path, type=TransactionOperationType.DELETE, if_version=if_version)
        return self.run_transaction([op], principal=principal, reason=reason, task_id=task_id)

    # ---- Commit (spec.md §9.1) --------------------------------------------------

    def _commit(self, *, principal: Principal, reason: str, task_id: str | None, files: list[str]) -> str:
        unique_files = list(dict.fromkeys(files))
        header = f"[user:{principal.user_id}] [client:{principal.client_id}]"
        if task_id:
            header += f" [task:{task_id}]"
        header += f" {reason}"
        body = (
            f"Files: {', '.join(unique_files)}\n"
            f"Timestamp: {datetime.now(UTC).isoformat()}\n"
        )
        commit = self._repo.index.commit(f"{header}\n\n{body}")
        return commit.hexsha

    def blob_version(self, path: str) -> str | None:
        """Öffentlicher Zugriff für Aufrufer außerhalb dieses Moduls (z.B. Audit-Middleware)."""
        return self._blob_version(path)
