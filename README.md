# agent-md-api

**A permissioned, audited, concurrency-safe HTTP API that lets multiple AI agents — and humans — work on the same tree of files without destroying each other's work.**

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

---

## The problem

You have several agents working on the same set of files.

Agent A reads `report.md`, thinks for thirty seconds, and writes it back. In the meantime Agent B has already rewritten the same file. Agent A's write lands on top and B's work is gone — silently, with no error, no conflict, no trace of what happened.

Hand agents a shared filesystem and you get a race condition without an error message. Hand them a git repository and you get merge conflicts they can't resolve, credentials you'd rather not distribute, and no way to say "this agent may read the invoices but must not touch the personnel files."

## What this does

`agent-md-api` sits between your agents and the files:

- **Optimistic concurrency control.** Every write carries the version the agent based its change on. If the file moved on, the write is rejected with `409` — and the response already contains the current content and version, so the agent can re-plan without another round trip.
- **`str_replace` with uniqueness check.** Patch-style edits require the target string to appear *exactly once*. Zero matches or several — the edit is refused instead of guessing.
- **Atomic multi-file transactions.** Validate everything, then write everything, then one commit. A transaction that fails partway writes nothing at all.
- **Two-axis access control.** Rules combine *who* (`user_id`), *through which client* (`client_id`), *which path*, and *which kind of file*. An agent can be allowed to read `/finance/` but never write it, and denied all `.pii.json` files everywhere regardless of path.
- **Audit that includes reads.** Every call is logged, not just the writes — with a `pii_accessed` flag whenever a PII-classified file is touched. "Who saw which personal data, and when" is a query, not an investigation.
- **Git as the history.** Each transaction becomes one commit with a mandatory `reason`, plus the acting user, client and task. `git log` is the audit trail for content.
- **A WebDAV bridge for humans.** Mount the same tree as a network drive and edit files in Explorer or Finder. Same permissions, same audit, same concurrency rules — humans are just another client, not an exception.
- **Metadata for binaries via a naming convention, not a special case.** Want structured metadata next to an image or other binary (e.g. `photo.jpg`)? Write it to `photo.jpg.meta.json` — an ordinary `kind: json` file, versioned and access-controlled like any other. No dedicated `kind`, no schema baked into the API: what fields belong in that JSON is entirely up to your application.
- **`kind: skill` files describe how to extract structured data, without the API ever executing anything.** A file family shares a base name across kinds — `deed.pdf` (the source), `deed.skill` (instructions for turning it into structured data), `deed.json` (the result). The API stores, versions, ACL-protects, and audits `.skill` files exactly like any other text file; running the extraction is entirely up to the calling agent or orchestration layer.

## Quick start

Requires Python 3.11+ and an existing git working tree to serve.

```bash
pip install -e .

mkdir -p /tmp/tree && cd /tmp/tree && git init && echo "# Hello" > note.md
git add -A && git commit -m "initial"
```

Register a client:

```bash
agent-md-api --registry-path /tmp/state/clients.json create-client my-agent \
    --type autonomous-agent --fixed-user-id system:my-agent
```

The API key is printed once — save it. See `agent-md-api --help` for `list-clients`, `revoke-client`, and `rotate-key`.

Run it:

```bash
export AGENT_API_ROOT_DIR=/tmp/tree
export AGENT_API_AUDIT_BACKEND=sqlite
export AGENT_API_AUDIT_DB_PATH=/tmp/state/audit.db
export AGENT_API_CLIENT_REGISTRY_PATH=/tmp/state/clients.json
export AGENT_API_INSTANCE_LOCK_PATH=/tmp/state/instance.lock
export AGENT_API_ADMIN_USER_ID=system:my-agent

uvicorn agent_md_api.main:app --workers 1
```

```bash
curl -H "Authorization: Bearer $API_KEY" localhost:8000/api/v1/tree
curl -H "Authorization: Bearer $API_KEY" localhost:8000/api/v1/file/note.md
```

The full HTTP contract is in [`docs/openapi.yaml`](docs/openapi.yaml) (OpenAPI 3.1).

## Endpoints

| | |
|---|---|
| `GET /tree` | listing with `root`, `depth`, `cursor` — entries you may not read are hidden, not marked |
| `GET /file/{path}` | text content + version, or metadata for binaries |
| `GET /file/{path}/content` | raw bytes (binaries) |
| `POST /file/{path}/edit` | `str_replace` with `old_str` / `new_str` / `if_version` |
| `POST /file/{path}/append` | append with `if_version` |
| `POST /file/{path}` | full write (JSON for text, multipart for binaries) |
| `DELETE /file/{path}` | requires `if_version` |
| `POST /transaction` | several operations, one commit, all or nothing |
| `/system/clients`, `/system/audit` | client registry and audit queries (admin only) |

## Two things worth knowing before you deploy

**Exactly one writer process.** The locking model assumes a single process owns the tree. That is a hard architectural constraint, not a tuning parameter — an in-process lock cannot protect against a second worker in a different process. The application takes an exclusive file lock on startup and **refuses to start** if another instance holds it. Run with `--workers 1` and do not scale the container horizontally against the same volume.

**Identity comes from headers, never from the body.** A client authenticates with an API key (`Authorization: Bearer …`). If it acts on behalf of a human, it also sends a short-lived EdDSA-signed token (`X-User-Token`) whose public key is looked up from the registry using the *already verified* client — not from the token itself. Autonomous agents skip the token and map to a fixed system identity.

## Known gaps

Honest list, so you find out here rather than later:

- **Binaries live in git.** Deliberate for now — content-addressable storage was considered and deferred. Large binary trees will grow the repository.
- **PII detection only covers `pii.json` today.** A binary file can't yet be flagged as PII the same way (imagine a photo showing identifiable people) — `kind` recognizes `pii.json` but not, say, `foto.pii.jpg`. A generic rule (any filename containing `.pii.` gets the PII treatment, regardless of extension) is designed but not implemented — see `docs/spec.md` §3/§7.1/§8.
- **WebDAV bridge:** `MOVE`/`COPY` are atomic for text files (one transaction), but not for binaries or when moving into a directory that does not exist yet. Directory `DELETE`/`MOVE` are not implemented.
- **Audit query** is only supported by the SQLite backend; `jsonl` is for shipping to Loki/ELK/CloudWatch and expects you to query there.
- **Documentation is partly in German.** The specification (`docs/spec.md`) and the source comments are German; the API contract and this README are English.

## Status

Used in production by one project. The API shape is stable and covered by ~145 tests across the core and the bridge, but this has not been exercised at scale or by anyone other than its author. Treat it as early software with a well-defined contract — issues and questions are welcome.

## Documentation

- [`docs/openapi.yaml`](docs/openapi.yaml) — the HTTP contract (OpenAPI 3.1)
- [`docs/spec.md`](docs/spec.md) — design specification with rationale *(German)*
- [`docs/deployment.md`](docs/deployment.md) — container setup, configuration, operational constraints *(German)*

## License

Apache License 2.0 — see [LICENSE](LICENSE).
