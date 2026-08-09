# Spec: Agent API

> Status: Design spec · Version 0.3
> Machine-readable counterpart: [docs/openapi.yaml](openapi.yaml).

## 1. Purpose

A **generic, cross-project reusable** API layer through which humans and AI agents access trees of Markdown/JSON/PII-JSON/binary files on equal footing — with optimistic concurrency control, atomic multi-file transactions, fine-grained access control, and a git-based audit trail.

Not a domain repo, no domain logic — pure infrastructure. Typical use: a web application (BFF) and several AI agents share access to the same file tree (e.g. a case file management system, a knowledge base, a project archive) — without writes silently overwriting each other, and without access rights being hard-wired into each individual client.

## 2. Scope of this repo

**The Agent API — and generic, domain-independent access bridges built on top of it.** The dividing line is not "just the Agent API," but **"no domain repo, no domain logic"** (§1): domain-coupled consumers (a web frontend, an orchestrator, a mobile app) live in their own projects. The **WebDAV bridge** (§14), on the other hand, has **no content coupling to domain data** — it translates a generic protocol (WebDAV) into generic Agent API calls, just as the Agent API itself is generic. That's why it lives **in this repo**, even though it is technically another type of BFF.

```
Domain-coupled consumers               Generic consumers (this repo)
(own projects)                         ┌─────────────────────────┐
Web frontend, later                    │  WebDAV bridge (§14)     │
mobile app/orchestrator/               │  Explorer/network drive  │
autonomous agents                      └────────────┬────────────┘
        │                                            │
        │  client API key (+ optional X-User-Token, §10)
        ▼                                            ▼
                    Agent API  (this repo)
                              │
                              ▼
      Storage: text in the git working tree; binaries also in git for now (§7)
```

## 3. Data model

- `kind`: `dir` | `md` | `json` | `pii.json` | `skill` | `binary` | `pii.binary`
- Version token per file: hash/timestamp for text kinds, hash of the bytes for `binary`/`pii.binary`
- Storage backend: git working tree. Every transaction produces exactly one commit (audit, §9)

**File families and `.skill` files (implemented):** Several files sharing the same base name but different `kind`s form a **file family** — e.g. `deed.pdf` (`binary`, the source), `deed.skill` (extraction instructions) and `deed.json` (the result). Purely a naming convention, not its own data structure: the Agent API knows no link between the files in a family beyond the shared prefix.

A `.skill` file describes **how** the structured variants (`json`/`pii.json`/`md`) of a family are derived from the underlying document — not the result itself. Purely descriptive, **no execution engine in the Agent API**: `kind: skill` files are stored, versioned, ACL-protected, and audited (§8/§9) exactly like any other text file — the actual extraction is run by the calling agent or the orchestration layer on top, never by the Agent API itself (§1: no domain logic).

A file family refers to **0–n source documents**, typically exactly one:
- **1 document** (the common case): an uploaded binary file (e.g. a scanned land registry excerpt). Highly structured documents can often be extracted (nearly) completely; individually worded ones (e.g. insurance policies) at least yield a fixed core set of fields.
- **0 documents**: the skill instead describes, say, questions to ask a human, or research against external sources (the web, other systems) — there's no binary file in the family for it to refer to.
- **n documents**: several sources contribute to one extraction.

Which source(s) a skill actually refers to is free text inside the skill's own content — the Agent API neither validates nor tracks it. No separate `PII_SKILL` kind: instructions aren't personal data themselves, even when they describe how to extract PII (matching `md`, which likewise has no PII variant of its own — only `json` and `binary` have one, via `pii.json`/`pii.binary`).

**Typical trigger** (outside the Agent API): a new binary file gets uploaded into a family that already has a `.skill` — the orchestration layer notices (e.g. via a new `write` audit entry with `kind: binary` in a directory that has a matching `.skill`, §9) and kicks off extraction.

**Generic filename-based PII detection, not hardcoded per kind:** `pii.json` used to be the only special case (`path.endswith(".pii.json")`, `storage/git_repo.py::classify_kind`). Generalized into one rule: **any filename containing the `.pii.` infix** is PII-classified, regardless of the actual extension. `report.pii.json` → `pii.json` (unchanged), `photo.pii.jpg` → `pii.binary` (new), anything else without the `.pii.` infix → `json`/`md`/`binary` as before. Reason: unlike JSON fields, image content can't be split field-by-field into PII/non-PII — a photo showing identifiable people is PII as a *whole file*, so that has to show up at the file's `kind` itself, not only in a sidecar (§7.1). Side effect: a sidecar belonging to the original, e.g. `photo.pii.jpg.json`, matches automatically too — consistently restrictive, no special case needed.

Kind gate (§8) and the PII audit flag (§9.2) apply to `pii.binary` exactly as they do to `pii.json` — every place currently checking `kind is PII_JSON` needs `kind in {PII_JSON, PII_BINARY}` (`api/files.py`, `api/tree.py`, `api/transactions.py`).

> **Status: specified, not yet implemented.** Implementation (kind enum, `classify_kind`, every `PII_JSON` comparison site, tests) is an open next step.

## 4. Read path

```
GET /tree?root={path}&depth={n}&cursor={token}
```
- `root` (default `/`): only return this subtree
- `depth` (default 2): levels below `root` (`0` = only metadata for `root`, `1` = only direct children, …)
- `cursor`: pagination per directory level for many entries
- Response per entry: `path`, `kind`, `size`, `version`, `preview` (text kinds only), `mime_type` (`binary` only)
- **ACL-filtered:** entries without read access are **fully hidden**, not just content-locked (§10)

```
GET /file/{path}          Text: full content + version · Binary: metadata only (version/size/mime_type)
GET /file/{path}/content  binary only: raw bytes/download (no text extraction — stays outside this API)
```

Aggressively cacheable (`ETag`/`If-None-Match`) — reads ≫ writes.

## 5. Write path

Every write call carries mandatory fields: `user_id`, `client_id` (from auth, §10), `reason`, optional `task_id`.

**Text (`md`/`json`/`pii.json`):**

| Endpoint | Semantics |
|---|---|
| `POST /file/{path}/edit` | `old_str`/`new_str`/`if_version` — `old_str` must occur **exactly once** in the current content (0 or >1 matches = error). Double safeguard: version check **and** exact string match |
| `POST /file/{path}/append` | no `old_str` needed, only `if_version` |
| `POST /file/{path}` | full write — new file or major restructuring |
| `DELETE /file/{path}` | only with `if_version`, always explicit |

**Binary (`binary`, e.g. Word/PDF/images):** no `str_replace` — old/new makes no sense.

| Endpoint | Semantics |
|---|---|
| `POST /file/{path}` | full replace/upload, `if_version` = hash of the bytes |
| `DELETE /file/{path}` | with `if_version` |

**Conflict case:** `409` returns the current content + current version (§11).

## 6. Transactions

**Single-call transaction** (the common case). `user_id`/`client_id` are **not** in the body — like every other write call they are verified server-side from the auth headers (§10), never taken from client-supplied data (otherwise a client could impersonate another user):

```
POST /transaction
{ "task_id": "…", "reason": "…",
  "operations": [ {path, type, old_str, new_str, if_version}, … ] }
```

Two-phase: (1) validate all operations against current versions/string matches/**ACL**, write nothing yet; (2) only once **all** are valid, write all files + **one** git commit covering all affected files.

Serialized through a **global, serial write queue** — sufficiently performant for a manageable number of concurrently active agents with a bursty/block-wise access pattern (e.g. 5 requests, then a 1–2h pause), **no** DB with row versioning needed. Only upgrade this once queueing times actually demonstrate a real problem.

> **Architectural boundary: exactly one writer process.** The entire locking model of this spec (write queue, OCC safety between version check and write, read-blocking during the write phase) only holds as long as **exactly one** process per instance writes exclusively to the git working tree — see the locking implementation below. This is not a performance optimization, it is a **fundamental modeling assumption**: an in-process lock only protects within the same process memory. Should more than one writer process against the same tree ever become necessary (multi-worker, horizontal replicas, multiple instances against the same tree), a configuration change is **not** enough — that requires an actual rebuild of the locking mechanism (e.g. a distributed lease/lock via an external coordinator, or reverting to a DB with row versioning, cf. §6 above). Until then: a single writer process is a hard requirement, not an implementation detail.

**Locking implementation:**
- The write queue is a genuine **in-process critical section** (e.g. an `asyncio.Lock` or a single-consumer worker) that fully wraps phase 1 (validation) + phase 2 (file writes + git commit) of a transaction — not just the write itself. This means no other transaction can change the version between the version check and the write; **no per-file lock files needed.**
- Precondition: **exactly one** worker process per instance (no multi-worker/multi-replica against the same git working tree) — otherwise the in-process lock is ineffective. Hard-enforced at startup (e.g. abort if more than one worker process is configured), so the single-instance guarantee doesn't silently break in deployment.
- **Reads during the write phase:** `GET /tree`/`GET /file` are also briefly deferred through the same lock during a transaction's (short) write phase, so no reader ever sees an intermediate state (file 1 new, files 2/3 still old). Matches the expected access pattern (bursty, then pauses) — the lock time is negligible.
- **Later option (if needed):** instead of locking reads, make commits atomic via git plumbing — write new blobs, build the tree object, create the commit object, atomically repoint the branch ref only at the very end, then `checkout`. Readers would then always see either the complete old or the complete new state, with no read lock at all. More implementation effort (GitPython low-level API instead of simple file writes) — only implement once read locks actually become a real problem.

Multi-call lifecycle (`begin`/`edit`/`commit`/`abort`, incl. TTL+reaper job) is not needed for the initial launch — only once there is real demand.

## 7. Binary files: storage decision (phase 1)

Binary files stay **in git for now**, despite known downsides (repo growth, no meaningful diffs) — content-addressable storage outside of git was discussed and rejected because the risk of not being able to find files directly in an emergency would undermine trust in the storage.

**Later option** (if repo growth becomes too large, e.g. an images folder): local CAS with hash-sharded directories + a manifest file per path in git, no third party needed.

### 7.1 Metadata for binary files (e.g. images) — sidecar convention, no new `kind`

No dedicated `kind` and no special field is introduced in the agent API for metadata on binary files — that would pull domain logic (what counts as "useful" image metadata) into this domain-agnostic repo (§2). Instead: a plain naming convention on top of the existing data model.

- Next to `<path>/<file>.<ext>` (`kind: binary`) an optional `<path>/<file>.<ext>.meta.json` (`kind: json`) can live in the same directory — an ordinary, independently versioned/ACL-protected JSON file, no API special case.
- `GET /tree` lists both entries side by side; an agent reads/writes the meta JSON through the normal `GET|POST /file/{path}` endpoints.
- The **schema** of the meta JSON is defined and owned by the consumer, not this repo.
- **PII on binary files** (e.g. a photo showing identifiable people): not a field in the sidecar, but on the **original file's name** itself (`.pii.` infix, e.g. `photo.pii.jpg`) — see §3 for the generic detection rule and why this has to be kind-scoped rather than field-scoped for image content.

## 8. Access rights — two-axis identity × path × kind

Two independent identities per request:
- **`user_id`** — who is responsible (a human, e.g. `human:alice`, or a system account for fully autonomous agents, e.g. `system:planner-03`)
- **`client_id`** — through which channel (`web-app`, `mobile-app`, `orchestrator`, …)

Reason: a client is called by multiple users, and the same user should be able to have different visibility across different clients (web vs. mobile).

**ACL rules per scope**, combined by intersection (both must allow):
```json
[
  {"scope": "user", "user_id": "human:alice", "path_prefix": "/finance/", "read": "allow", "write": "allow"},
  {"scope": "client", "client_id": "mobile-app", "path_prefix": "/finance/", "read": "deny", "write": "deny"},
  {"scope": "client", "client_id": "web-app", "path_prefix": "/", "read": "allow", "write": "allow"}
]
```
- Effective rule for `(user, client, path)`: a specific **pair rule** (user+client combination) wins if one exists; otherwise the **most restrictive** decision among the user, client, and global rules (a rule with neither `user_id` nor `client_id` applies to every principal).
- **Default with no matching rule: fail-closed (`deny`)** — as soon as any `_system/acl.json` exists in the tree (even with `[]` content), every combination of `user_id`/`client_id`/path/`kind` it doesn't cover is denied, not allowed. An incomplete or buggy ACL configuration must never accidentally grant access to an unknown principal. **The only exception:** if **no** `acl.json` exists yet at all (fresh tree, bootstrap), the default stays allow — otherwise not even the admin could write the very first ACL file, since that write goes through the same check (`/_system/acl.json` is additionally hard-restricted to the admin `user_id`, §10a — but on top of the normal ACL check, not instead of it).
- A `kind` rule applies **additionally and independently of the path** (e.g. a blanket write ban for all `pii.json`, regardless of location) — combined additively with AND against the scope decision, `deny` wins. Applies equally to `pii.binary` (§3).
- The first matching, **more specific** rule wins (path prefix before the general `/`).
- Enforced on `GET /tree` (hiding entries) **and** on every individual read/write call.

**Glob patterns in `path_prefix`:** A pattern without `*` remains a plain literal prefix as above. If it contains `*`, it is instead matched as a glob against the **full path** (not just as a prefix) — `*` matches any characters except `/` (stays within a path segment), `**` also matches across `/`. This lets you lock an entire document across all `kind` variants and then selectively reopen it:

```json
[
  {"path_prefix": "lease*.*", "write": "deny"},
  {"path_prefix": "lease.json", "write": "allow"}
]
```

`lease.json` is writable, `lease.pii.json` and `lease.md` stay locked — the more specific rule (more literal characters, see `_specificity_score` in `acl/engine.py`) only wins for the path it exactly matches. A glob is deliberately a **full match**, not a prefix match: `lease*.md` matches `lease.md`, but not `lease.md.bak`.

**Orchestrator case:** authenticates itself as its own `client_id`, but passes the `user_id` claim of the triggering human through the entire chain (frontend → orchestrator → Agent API) — every call gets the rights of the actually triggering user, never the orchestrator's own rights across the board.

**ACL management:** a versioned JSON file in the same tree (`/_system/acl.json`), managed through the same API — write access restricted to one fixed admin `user_id`, independent of the other path/kind rules. No separate config service.

## 9. Audit

### 9.1 Writes — git commit

- **One commit per transaction**, no time-based batching.
- `reason` is a mandatory parameter, supplied by the triggering client. The server automatically enriches it with: `user_id`, `client_id`, task ID, timestamp, session ID, affected files.
- No after-the-fact generation of `reason` via an LLM from the diff.
- Optional: daily snapshots/tags for a rough overview, without sacrificing granular commits.

### 9.2 Reads — also fully logged

Not just writes — **every read call** (`GET /tree`, `GET /file/{path}`, `GET /file/{path}/content`) is logged too. Specially flagged: any access — read **or** write — to a `kind: pii.json` file sets `pii_accessed: true` (§9.3), so that "who saw which personal data, and when" can be answered at any time.

**Why reads can't go through git commits:** A commit is meant for a content change — a read changes nothing, there would be no meaningful commit content. Given "reads ≫ writes" (§4), pure read noise would also make the commit history unusable for its actual purpose (content history). Reads necessarily need their **own** storage — see §9.3.

### 9.3 Audit log storage — configurable port

No hard-wired storage, but a **port with swappable adapters** (analogous to other extraction/storage ports: the system only knows the interface, not the mechanism). The rest of the Agent API only knows `AuditSink.record(entry) -> None` (and optionally `.query(filters) -> AuditLogResponse` — not every adapter supports queries, see below), not the concrete backend.

**Configuration:** env var `AGENT_API_AUDIT_BACKEND` = `none | sqlite | jsonl | loki | elk | azure | aws`.

| Backend | Status | How it works |
|---|---|---|
| `none` | **built** | No-op — no overhead, but also no compliance benefit. Intended for dev/test only, not production. |
| `sqlite` | **built** | Its own SQLite file, WAL mode, append-only. **Only adapter with real `query()` support** — feeds `GET /system/audit` (§9.4) directly. |
| `jsonl` | **built** | Structured JSON lines to stdout/file, **no own network client**. Covers Loki/ELK/Azure/AWS via an **external standard shipper** picking up the lines: Promtail → Loki, Filebeat/Logstash → ELK, Azure Monitor Agent → Log Analytics, CloudWatch Agent → CloudWatch Logs. `query()` **not supported** — evaluation happens in the respective external tool (Grafana/Kibana/Azure Monitor/CloudWatch Insights), not via `GET /system/audit`. |
| `loki`/`elk`/`azure`/`aws` as **native SDK push adapters** | **not built** | Only if `jsonl` + shipper agent isn't enough — direct push from the app, no intermediate step. |

**No contradiction** to the "no DB needed" decision in §6 — that one concerned content versioning/OCC (read-modify-write with conflict logic). Audit logging is purely additive for every adapter (append only, no conflicts) — a different problem.

**Entry per call** (backend-independent schema): `id`, `timestamp`, `user_id`, `client_id`, `operation` (`tree`|`read`|`read_content`|`edit`|`append`|`write`|`delete`|`transaction`), `path` (or a list of paths for `transaction`), `kind`, `pii_accessed` (bool), `commit_id` (nullable, writes only — reference to the git commit for diff details), `reason` (nullable, writes only), `result` (`success`|`denied`|`error`), `task_id` (nullable).

- **Location (for `sqlite`/`jsonl`):** its own path outside the git working tree (`AGENT_API_AUDIT_DB_PATH` or `AGENT_API_AUDIT_LOG_PATH`, see [deployment.md](deployment.md)) — audit data doesn't belong in versioned content, otherwise every read log entry would itself trigger another content change (and thus another read that needs logging).
- **Append-only, immutable** — no UPDATE/DELETE, corrections (if ever needed) as a new entry.
- **Written synchronously**, before the response — at this volume there's no noticeable latency impact; otherwise there's a risk that an actually-occurred read never gets logged (not an acceptable risk for a compliance purpose).
- **Middleware instead of individual calls:** a FastAPI middleware layer calls `AuditSink.record()` exactly once after every request completes (including the result status) — prevents forgotten log calls in individual endpoint handlers, and turns switching adapters into a pure configuration matter.
- **Volume/retention** (non-binding, later refinement): with very frequent `GET /tree` polling (e.g. by the WebDAV bridge, §14) the log can grow large — not critical for SQLite/JSONL as such, but a retention/archiving rule makes sense at some point.

### 9.4 Query

`GET /system/audit` (admin-only, see [openapi.yaml](openapi.yaml)) with filters `user_id`, `path_prefix`, `pii_only` (bool), `from`/`to`, `operation` — directly covers the compliance case "show me all PII accesses by X in period Y", **provided the configured adapter supports `query()`** (today: only `sqlite`). With `jsonl`/`none`/the later cloud adapters, the endpoint returns a clear message ("query not supported for backend X, evaluate via <external tool>") instead of an empty or misleading result.

## 10. Authentication

Two separate mechanisms — an API key alone only proves the client, not the human behind it:

**a) Client authentication (API key + secret)**
Every registered client gets its own API key (Agent API's client registry, key stored hashed, rotatable/revocable). `Authorization: Bearer <client_api_key>` — the Agent API derives the real `client_id` from this, never taken from the body.

**b) User identity (signed short-lived token)**
After a human logs in, the respective client (e.g. the web frontend) issues a short-lived, signed token (`user_id`, `client_id`, `exp` ~15 min, EdDSA/Ed25519 with the client's private key). Header `X-User-Token: <JWT>`. The Agent API verifies the signature with the **public key from the registry** (not from the token itself — otherwise a client could impersonate another one), checks `iss == verified client_id`, `exp`, key not `revoked`.

Autonomous agents with no human behind them: their own API key, mapped directly to a system `user_id` (`fixed_user_id` in the registry) — no delegation step, no token needed.

| Level | Mechanism | Proves |
|---|---|---|
| Client (service) | API key + secret, managed locally | The request really comes from the stated client |
| User (human) | Signed short-lived token after login | The call is really on behalf of the stated user |
| Autonomous agent without a human | Own API key = direct identity | No delegation step needed |

Token renewal is up to the respective client (session/refresh handling is outside the Agent API's scope).

## 11. Conflict escalation

- `409` returns the current content + current version, so the agent can re-plan without another `GET`.
- **Automatic retry:** up to **3** attempts, each with a fresh version + a newly computed patch.
- **After 3 failed attempts:** escalation as a conflict file in the tree under `/_conflicts/{original_path}.conflict.{timestamp}.json`:
  ```json
  {
    "conflict_id": "cf_8a2f",
    "original_path": "/notes/pricing.md",
    "user_id": "human:alice",
    "client_id": "web-app",
    "task_id": "pricing-update-42",
    "reason": "Price updated",
    "attempted_version": "v9",
    "attempted_old_str": "Price: TBD",
    "attempted_new_str": "Price: €4,200",
    "current_content_at_escalation": "...",
    "retry_count": 3,
    "created_at": "2026-08-04T10:15:00Z",
    "status": "open"
  }
  ```
- **Daily cleanup job:** clears out resolved/stale conflict files; **actively notifies** (Slack/email) about newly occurred, still-unresolved conflicts since the last run.
- No server-side auto-merge. Manual resolution happens through the respective client's UI (outside this repo) — a human decides: reapply, discard, or manually merge.

## 12. Design decisions at a glance

- The repo contains the Agent API plus generic, domain-independent bridges on top of it (§2); domain-coupled frontends live in their own projects.
- Tree read with pagination (`root`/`depth`/`cursor`), binary file handling, two-axis ACL (user×client×path×kind), client+user auth, conflict escalation with a cleanup job.
- Binary files stay in git for phase 1 (§7), CAS noted as a later option.
- Tech stack: Python + FastAPI + GitPython + `cryptography`/PyJWT for EdDSA.
- **Multi-project operation:** **one instance per project/tree**, separate deployment per consumer — no multi-tenant model. The ACL/registry model (§8/§10) is accordingly designed for **one** tree per instance.
- `user_id`/`client_id` come exclusively from the verified auth headers (§10), never from the request body (otherwise identity spoofing would be possible) — an early design draft had this inconsistent in one place.
- Deployment form: built in isolation, but run as its **own process/container within the same deployment bundle** as the consuming frontend (sidecar, addressed over HTTP with a client API key/JWT) — **not** imported as an in-process library, so that the serial write queue/OCC (§6/§8) retains its single-instance guarantee even with multiple frontend workers/replicas.
- Reads are audited, PII accesses flagged separately (§9.2).
- Audit log as a configurable port (§9.3): `none|sqlite|jsonl|loki|elk|azure|aws`, built: `none`+`sqlite`+`jsonl` (the latter covers Loki/ELK/Azure/AWS via external standard shippers). Native SDK push adapters for Loki/ELK/Azure/AWS not planned for now.
- WebDAV bridge (§14): complete design (architecture, auth translation, protocol mapping, proposed tech stack `wsgidav`). Stays in this repo (generic, no domain coupling).
- Glob patterns in `path_prefix` (§8): `*`/`**` supported, allows e.g. locking a document across all `kind` variants and then selectively reopening individual kinds. Field name deliberately unchanged (no schema break) — patterns without `*` remain plain literal prefixes.
- Metadata for binary files (§7.1): sidecar convention (`<file>.<ext>.meta.json`), no new `kind`, no schema knowledge in this repo.
- `pii.binary` kind + generic `.pii.` infix detection (§3/§7.1/§8): specified, **not yet implemented** — `classify_kind`, the `Kind` enum, and every `PII_JSON` comparison site still need updating.

## 13. Backlog (non-binding)

Four possible later extensions, none of which blocks the current state — prompted by comparison with similar multi-agent coordination projects:

1. **Foreign-edit guards** — detect whether someone has written directly to the git working tree, bypassing the API.
2. **Snapshot sessions / read-skew protection** — for multi-file reads via `GET /tree`, so far only addressed for writes (§6).
3. **Invalidation instead of re-fetch** — a small signal instead of a full re-fetch for frequent polling.
4. **Effect-ordering gate pattern** (decide → re-check before effect → fire/hold) for cases where an agent derives a later-effective decision from a read.

## 14. WebDAV bridge

Humans continue to access files **via Explorer/Finder** (a WebDAV drive), rather than being forced through a web UI. Architecturally just another client type, but stays **in this repo** (§2) — no content coupling to domain data, purely generic protocol translation.

### 14.1 Architecture

```
Workstation                             Server (this repo)
Explorer/Finder ──WebDAV/HTTPS──> Reverse proxy (TLS) ──> WebDAV bridge ──> Agent API
```

**Important difference from the sidecar pattern (§12):** unlike frontend↔Agent API (typically on the same host, only `127.0.0.1`), the WebDAV bridge **must be reachable from outside the server** — it is mounted from the client PC, not from a process on the same host. So: its own public (or VPN-restricted) HTTPS endpoint with TLS (reverse proxy, Let's Encrypt) — there is no "internal-only reachability" safety net here, basic-auth protection (§14.2) carries the full load.

### 14.2 Auth translation — the bridge handles the headers itself

The built-in WebDAV client of Windows/macOS can **only send HTTP Basic Auth**, no custom headers. The bridge handles the translation completely, transparently to the client:

1. The bridge is itself a registered Agent API client (`client_id: webdav-bridge`, `issues_user_tokens: true`, its own signing keypair, §10).
2. The bridge maintains its **own, small access management** (username → `user_id` + hashed personal access token as the "password") — admin-only, independent of the Agent API client registry, since it's a pure human-access layer *in front of* the bridge.
3. On every incoming WebDAV request: check basic-auth credentials → resolve `user_id` → mint a user token (short-lived, possibly cached for the session) → issue the outgoing Agent API call with `Authorization: Bearer <bridge_client_api_key>` + `X-User-Token: <JWT>`.

From the Explorer/Finder's point of view it's a normal WebDAV drive with a username/password.

### 14.3 Protocol mapping

| WebDAV | Agent API |
|---|---|
| `PROPFIND` (directory) | `GET /tree?root=…&depth=1` |
| `PROPFIND` (file) | `GET /file/{path}` (metadata) |
| `GET` | `GET /file/{path}` (text) or `GET /file/{path}/content` (binary) |
| `PUT` | `POST /file/{path}` — `if_version` from a `GET` the bridge performs itself beforehand (the client supplies no version) |
| `DELETE` | `DELETE /file/{path}` with a previously determined `if_version` |
| `MKCOL` | no Agent API equivalent needed — directories arise implicitly from file paths; the bridge answers `MKCOL` as success with no action of its own |
| `MOVE`/`COPY` | **atomic via `POST /transaction`**: the bridge reads the source's content+version, sends a transaction with `write` (target path, content) plus, for `MOVE`, a `delete` (source path, `if_version`) — both operations in one commit (§6), no special case needed. **Limitation:** `/transaction` so far only knows text `content` (string) — so for `kind: binary` it is (for now) **not atomic**: three separate calls (read metadata+bytes, `POST` to the target path, `DELETE` on the source path). Deliberately documented openly, not silently generalized — extending `/transaction` with binary support (`content_base64`) is a backlog item, if this becomes a real problem in practice. |
| `LOCK`/`UNLOCK` | not supported (no-op rejection) — OCC remains the only safety net (§11), until a real need for locking shows up |

### 14.4 `reason` field

Automatically generated (e.g. `"Edited via WebDAV"`), since Explorer/Finder has no way to input one — an accepted quality loss for this access path, and like any automatically generated reason in this spec, not "polished up" after the fact by an LLM (§9.1).

### 14.5 Conflict behavior

`409` is passed through to the client (a generic error, no merge UI) — OCC remains **strictly enforced**, no silent last-write-wins. The conflict file is created just like for any other client (§11), regardless of the triggering access path — resolution can, if needed, happen through a more convenient web UI; WebDAV isn't the only access path.

### 14.6 Tech stack

`wsgidav` (Python, a mature WebDAV server library with a pluggable `DAVProvider`) instead of a custom WebDAV/XML protocol implementation — a custom `DAVProvider` translates into Agent API calls (`httpx`). Reduces the bridge to pure translation logic instead of protocol fiddling.
