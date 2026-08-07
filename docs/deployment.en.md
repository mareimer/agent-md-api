# Deployment

> Complements [spec.md](spec.md) §6 (single-instance guarantee) and §12/§14 (sidecar pattern for the frontend and the WebDAV bridge respectively). Covers operations only, no new domain logic.

## 1. Target picture

- The Agent API runs as a Docker container, typically on a Linux server/VPS.
- **Runs most simply on the same host as the consuming frontend** (sidecar pattern, spec.md §12) — no separate server needed unless there is an actual reason to split them.
- Exactly one Agent API instance, one worker process (single-instance guarantee, spec.md §6).
- Backup runs outside this repo/code, on its own infrastructure — documented here only as a constraint (§7).

## 2. Directory configuration

- **Env var `AGENT_API_ROOT_DIR`** — path *inside* the container to the git working tree, default `/data/repo`. The app expects an already-existing git working tree there (no automatic `git init` — migrating an existing set of files is its own upstream step, see §6).
- **Env var `AGENT_API_AUDIT_BACKEND`** — `none|sqlite|jsonl|loki|elk|azure|aws` (spec.md §9.3), default `sqlite`.
- **Env var `AGENT_API_AUDIT_DB_PATH`** — only with `AGENT_API_AUDIT_BACKEND=sqlite`: path to the SQLite file, default `/data/state/audit.db`.
- **Env var `AGENT_API_AUDIT_LOG_PATH`** — only with `AGENT_API_AUDIT_BACKEND=jsonl`: path to the JSON-lines file, default `/data/state/audit.jsonl` (alternatively stdout, in which case the Docker log driver takes over instead of a file).
- **Env var `AGENT_API_CLIENT_REGISTRY_PATH`** — path to the client registry JSON file (spec.md §10), default `/data/state/clients.json`. Contains hashed API keys — sensitive, but not git content.
- **Env var `AGENT_API_INSTANCE_LOCK_PATH`** — path to the lock file for the single-instance guarantee (spec.md §6), default `/data/state/instance.lock`. No backup needed (purely an operational lock, no state).
- **Env var `AGENT_API_ADMIN_USER_ID`** — the only `user_id` that has admin rights (`/system/clients`, `/system/audit`, write access to `/_system/acl.json`). **Without setting it, nobody has admin rights** (fail-closed) — deliberately no default pointing at a real account.
- Audit, client registry, and lock paths are deliberately **outside** `AGENT_API_ROOT_DIR` — those are operational data, not git content.
- **On the host side:** a shared bind-mount path for operational data is enough, e.g. `/srv/agent-api/state` (containing `audit.db`, `clients.json`, `instance.lock`), plus `/srv/agent-api/repo` for the git working tree. Deliberately **plain host directories**, not Docker named volumes — an external backup tool (§7) or an external log shipper (Promtail/Filebeat/Azure Monitor Agent/CloudWatch Agent, for `jsonl`) needs direct filesystem access to known paths.

## 3. docker-compose.yml

```yaml
services:
  agent-api:
    build: .
    volumes:
      - /srv/agent-api/repo:/data/repo
      - /srv/agent-api/state:/data/state    # audit DB, client registry, instance lock — not git content
    environment:
      - AGENT_API_ROOT_DIR=/data/repo
      - AGENT_API_AUDIT_BACKEND=sqlite
      - AGENT_API_AUDIT_DB_PATH=/data/state/audit.db
      - AGENT_API_CLIENT_REGISTRY_PATH=/data/state/clients.json
      - AGENT_API_INSTANCE_LOCK_PATH=/data/state/instance.lock
      - AGENT_API_ADMIN_USER_ID=human:you
    ports:
      - "127.0.0.1:8100:8000"     # reachable locally only, see §4
    command: ["uvicorn", "agent_md_api.main:app", "--host", "0.0.0.0", "--workers", "1"]
    restart: unless-stopped

  webdav-bridge:
    build: ./webdav-bridge
    depends_on:
      - agent-api
    environment:
      - AGENT_API_BASE_URL=http://agent-api:8000
      - AGENT_API_CLIENT_ID=webdav-bridge
      # AGENT_API_CLIENT_API_KEY, signing keypair etc. via secrets, not in plaintext here
    ports:
      - "127.0.0.1:8200:8000"     # NOT exposed directly — reverse proxy handles TLS, see §4.1
    restart: unless-stopped
```

`webdav-bridge` talks to `agent-api` internally via the Docker Compose service name (`http://agent-api:8000`) — no `127.0.0.1` binding is needed between the two containers for that, Compose provides its own internal network.

## 4. Network

- Port **bound to `127.0.0.1` only** — not reachable from outside (the public internet) as long as the frontend runs on the same host.
- The frontend talks to the Agent API via `http://127.0.0.1:8100`, with a client API key + user token as described in spec.md §10.
- No TLS/firewall effort needed for this connection as long as both stay on the same host.
- **If the Agent API and the frontend move to separate hosts:** this section needs to be revisited — the client API key/JWT auth then carries the full load, and TLS (reverse proxy, e.g. Caddy/nginx with Let's Encrypt) and firewall rules become necessary.

### 4.1 WebDAV bridge — different, must be publicly reachable (spec.md §14.1)

Unlike Agent API↔frontend: the WebDAV bridge is mounted from a **remote client machine**, not from a process on the same host. So:

- A reverse proxy (Caddy/nginx) terminates TLS for the bridge, with a Let's Encrypt certificate and a public (or VPN-restricted) DNS name, e.g. `webdav.<domain>`.
- The bridge container itself stays bound to `127.0.0.1` (see the Compose example in §3) — the reverse proxy is the only publicly reachable point, not the container directly.
- Basic-auth protection (spec.md §14.2) carries the full load here — there is no "internal-only reachability" safety net like there is for Agent API↔frontend.

## 5. Single-instance guarantee (operational)

Direct implementation of spec.md §6 at the deployment level:

- `--workers 1` in the Uvicorn startup command (see the Compose example above) — **not** optional.
- **Never start a second container replica** against the same bind mount — neither via `docker-compose up --scale agent-api=2`, nor via a second, independent Compose stack pointed at the same host path.
- Hard-enforced at app startup (code level, spec.md §6): the app takes an exclusive file lock (`AGENT_API_INSTANCE_LOCK_PATH`) on start and aborts with a clear error message if it is already held — prevents the single-instance guarantee from silently breaking in deployment.

## 6. Migrating an existing set of files (one-off)

1. Copy existing data to the target server via `rsync`/`scp`, into `/srv/agent-api/repo`.
2. There: `git init`, `git add -A`, an initial commit (`reason`: e.g. "Initial migration", `user_id`: the person doing the migration — there is no git history yet if the files previously existed as a plain folder structure without version control).
3. Start the container — from this point on the Agent API owns write authority for this directory (spec.md §2).
4. **Freeze the old source afterwards** (read-only/archived, no more active work there). Reason: a source that is still being edited live in parallel would inevitably diverge from the Agent API's state without the Agent API ever knowing — that undermines the "sole write authority" from spec.md §2/§9. All future access goes exclusively through the Agent API or its consumers.

This migration is a **one-off, standalone action** — not part of the ongoing deployment configuration.

## 7. Backup (outside this repo)

- Runs on its own infrastructure outside the code — documented here only as a constraint that motivates the directory choice in §2.
- **Must back up `.git/`**, not just the working copy — otherwise a restore loses the commit history (write audit, spec.md §9.1).
- **Must also back up `/srv/agent-api/state`** — otherwise a restore loses the read/PII audit log (spec.md §9.2/§9.3, if `sqlite`) and the client registry (spec.md §10); the latter is not critical for domain data, but without it all clients would have to be re-registered. The instance lock needs no backup (purely operational).
- Recommendation: regular snapshots of both bind-mount directories (`/srv/agent-api/repo` and `/srv/agent-api/state`) via the chosen tool (e.g. restic/borg).
