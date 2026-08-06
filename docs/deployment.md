# Deployment

> Ergänzt [spec.md](spec.md) §6 (Single-Instanz-Garantie) und §12/§14 (Sidecar-Muster für Frontend bzw. WebDAV-Bridge). Betrifft nur den Betrieb, keine neue Fachlogik.

## 1. Zielbild

- Agent-API läuft als Docker-Container, typischerweise auf einem Linux-Server/VPS.
- **Läuft am einfachsten auf demselben Host wie das konsumierende Frontend** (Sidecar-Muster, spec.md §12) — kein separater Server nötig, solange kein echter Grund für eine Trennung besteht.
- Genau eine Agent-API-Instanz, ein Worker-Prozess (Ein-Instanz-Garantie, spec.md §6).
- Backup läuft außerhalb dieses Repos/Codes, eigene Infrastruktur — hier nur als Randbedingung dokumentiert (§7).

## 2. Verzeichnis-Konfiguration

- **Env-Var `AGENT_API_ROOT_DIR`** — Pfad *innerhalb* des Containers zum Git-Arbeitsbaum, Default `/data/repo`. Die App erwartet dort einen bereits existierenden Git-Arbeitsbaum (kein automatisches `git init` — Migration eines Bestands ist ein eigener, vorgelagerter Schritt, siehe §6).
- **Env-Var `AGENT_API_AUDIT_BACKEND`** — `none|sqlite|jsonl|loki|elk|azure|aws` (spec.md §9.3), Default `sqlite`.
- **Env-Var `AGENT_API_AUDIT_DB_PATH`** — nur bei `AGENT_API_AUDIT_BACKEND=sqlite`: Pfad zur SQLite-Datei, Default `/data/state/audit.db`.
- **Env-Var `AGENT_API_AUDIT_LOG_PATH`** — nur bei `AGENT_API_AUDIT_BACKEND=jsonl`: Pfad zur JSON-Lines-Datei, Default `/data/state/audit.jsonl` (alternativ stdout, dann greift der Docker-Log-Treiber statt einer Datei).
- **Env-Var `AGENT_API_CLIENT_REGISTRY_PATH`** — Pfad zur Client-Registry-JSON-Datei (spec.md §10), Default `/data/state/clients.json`. Enthält gehashte API-Keys — sensibel, aber kein Git-Content.
- **Env-Var `AGENT_API_INSTANCE_LOCK_PATH`** — Pfad zur Lock-Datei der Ein-Instanz-Garantie (spec.md §6), Default `/data/state/instance.lock`. Kein Backup nötig (rein betriebliche Sperre, kein Zustand).
- **Env-Var `AGENT_API_ADMIN_USER_ID`** — der einzige `user_id`, der Adminrechte hat (`/system/clients`, `/system/audit`, Schreibzugriff auf `/_system/acl.json`). **Ohne Setzen hat niemand Adminrechte** (fail-closed) — bewusst kein Default, der auf ein reales Konto zeigt.
- Audit-, Client-Registry- und Lock-Pfad bewusst **außerhalb** von `AGENT_API_ROOT_DIR` — das sind Betriebsdaten, kein Git-Content.
- **Host-seitig:** ein gemeinsamer Bind-Mount-Pfad für Betriebsdaten reicht, z.B. `/srv/agent-api/state` (darunter `audit.db`, `clients.json`, `instance.lock`), plus `/srv/agent-api/repo` für den Git-Arbeitsbaum. Bewusst **einfache Host-Ordner**, keine Docker-Named-Volumes — ein externes Backup-Tool (§7) bzw. ein externer Log-Shipper (Promtail/Filebeat/Azure Monitor Agent/CloudWatch Agent, bei `jsonl`) braucht direkten Filesystem-Zugriff auf bekannte Pfade.

## 3. docker-compose.yml

```yaml
services:
  agent-api:
    build: .
    volumes:
      - /srv/agent-api/repo:/data/repo
      - /srv/agent-api/state:/data/state    # Audit-DB, Client-Registry, Instance-Lock — kein Git-Content
    environment:
      - AGENT_API_ROOT_DIR=/data/repo
      - AGENT_API_AUDIT_BACKEND=sqlite
      - AGENT_API_AUDIT_DB_PATH=/data/state/audit.db
      - AGENT_API_CLIENT_REGISTRY_PATH=/data/state/clients.json
      - AGENT_API_INSTANCE_LOCK_PATH=/data/state/instance.lock
      - AGENT_API_ADMIN_USER_ID=human:you
    ports:
      - "127.0.0.1:8100:8000"     # nur lokal erreichbar, siehe §4
    command: ["uvicorn", "agent_md_api.main:app", "--host", "0.0.0.0", "--workers", "1"]
    restart: unless-stopped

  webdav-bridge:
    build: ./webdav-bridge
    depends_on:
      - agent-api
    environment:
      - AGENT_API_BASE_URL=http://agent-api:8000
      - AGENT_API_CLIENT_ID=webdav-bridge
      # AGENT_API_CLIENT_API_KEY, Signing-Keypair etc. über Secrets, nicht hier im Klartext
    ports:
      - "127.0.0.1:8200:8000"     # NICHT direkt nach außen — Reverse Proxy übernimmt TLS, siehe §4.1
    restart: unless-stopped
```

`webdav-bridge` spricht `agent-api` intern über den Docker-Compose-Service-Namen an (`http://agent-api:8000`) — dafür braucht es kein `127.0.0.1`-Binding zwischen den beiden Containern, Compose stellt ein eigenes internes Netz bereit.

## 4. Netzwerk

- Port **nur auf `127.0.0.1` gebunden** — von außen (öffentliches Internet) nicht erreichbar, solange das Frontend auf demselben Host läuft.
- Das Frontend spricht die Agent-API über `http://127.0.0.1:8100` an, mit Client-API-Key + User-Token wie in spec.md §10 beschrieben.
- Kein TLS/Firewall-Aufwand für diese Verbindung nötig, solange beide auf demselben Host bleiben.
- **Falls Agent-API und Frontend auf getrennte Hosts wandern:** dieser Abschnitt muss überarbeitet werden — dann trägt die Client-API-Key/JWT-Auth die volle Last, zusätzlich TLS (Reverse Proxy, z.B. Caddy/nginx mit Let's Encrypt) und Firewall-Regeln nötig.

### 4.1 WebDAV-Bridge — abweichend, muss öffentlich erreichbar sein (spec.md §14.1)

Anders als Agent-API↔Frontend: die WebDAV-Bridge wird von einem **entfernten Client-Rechner** aus gemountet, nicht von einem Prozess auf demselben Host. Also:

- Reverse Proxy (Caddy/nginx) terminiert TLS für die Bridge, Let's-Encrypt-Zertifikat, öffentlicher (oder VPN-beschränkter) DNS-Name, z.B. `webdav.<domain>`.
- Bridge-Container selbst bleibt `127.0.0.1`-gebunden (siehe Compose-Beispiel §3) — der Reverse Proxy ist die einzige öffentlich erreichbare Stelle, nicht der Container direkt.
- Basic-Auth-Absicherung (spec.md §14.2) trägt hier die volle Last — kein „nur intern erreichbar"-Sicherheitsnetz wie bei Agent-API↔Frontend.

## 5. Single-Instanz-Garantie (operativ)

Direkte Umsetzung von spec.md §6 auf Deployment-Ebene:

- `--workers 1` im Uvicorn-Start (siehe Compose-Beispiel oben) — **nicht** optional.
- **Keine zweite Container-Replica** gegen denselben Bind-Mount starten — weder über `docker-compose up --scale agent-api=2`, noch über einen zweiten, unabhängigen Compose-Stack auf denselben Host-Pfad.
- Beim App-Start hart abgesichert (Code-Ebene, spec.md §6): Die App nimmt beim Start einen exklusiven Datei-Lock (`AGENT_API_INSTANCE_LOCK_PATH`) und bricht mit klarer Fehlermeldung ab, wenn dieser bereits gehalten wird — verhindert, dass die Ein-Instanz-Garantie still im Deployment bricht.

## 6. Migration eines Bestands (einmalig)

1. Vorhandene Daten per `rsync`/`scp` auf den Zielserver kopieren, nach `/srv/agent-api/repo`.
2. Dort: `git init`, `git add -A`, initialer Commit (`reason`: z.B. „Initiale Migration", `user_id`: die Person, die migriert — es gibt noch keine Git-Historie, falls die Dateien vorher als reine Ordnerstruktur ohne Versionierung vorlagen).
3. Container starten — ab hier übernimmt die Agent-API die Schreibautorität für dieses Verzeichnis (spec.md §2).
4. **Die alte Quelle danach einfrieren** (read-only/archiviert, kein aktives Arbeiten mehr dort). Grund: eine weiterhin live editierte Parallelquelle würde zwangsläufig gegenüber dem Agent-API-Stand divergieren, ohne dass die Agent-API davon erfährt — das unterläuft die „alleinige Schreibautorität" aus spec.md §2/§9. Künftiger Zugriff ausschließlich über die Agent-API bzw. deren Konsumenten.

Diese Migration ist eine **einmalige, eigene Aktion** — kein Teil der laufenden Deployment-Konfiguration.

## 7. Backup (außerhalb dieses Repos)

- Läuft mit eigener Infrastruktur außerhalb des Codes — hier nur als Randbedingung, die die Verzeichnis-Wahl in §2 begründet.
- **Muss `.git/` mitsichern**, nicht nur die Arbeitskopie — sonst geht bei einem Restore die Commit-Historie (Schreib-Audit, spec.md §9.1) verloren.
- **Muss auch `/srv/agent-api/state` mitsichern** — sonst gehen bei einem Restore das Lese-/PII-Audit-Log (spec.md §9.2/§9.3, falls `sqlite`) und die Client-Registry (spec.md §10) verloren; Letztere ist unkritisch für die Fachdaten, aber ohne sie müssten alle Clients neu registriert werden. Der Instance-Lock braucht kein Backup (rein betrieblich).
- Empfehlung: regelmäßiger Snapshot beider Bind-Mount-Verzeichnisse (`/srv/agent-api/repo` und `/srv/agent-api/state`) durch das gewählte Tool (z.B. restic/borg).
