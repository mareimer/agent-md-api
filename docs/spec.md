# Spec: Agent-API

> Status: Design-Spec · Version 0.3
> Maschinenlesbares Gegenstück: [docs/openapi.yaml](openapi.yaml).

## 1. Zweck

Ein **generischer, projektübergreifend wiederverwendbarer** API-Layer, über den Menschen und KI-Agenten gleichberechtigt auf Bäume aus Markdown-/JSON-/PII-JSON-/Binärdateien zugreifen — mit Optimistic Concurrency Control, atomaren Mehrdateien-Transaktionen, feingranularer Zugriffskontrolle und git-basiertem Audit-Trail.

Kein Fach-Repo, keine Domänenlogik — reine Infrastruktur. Typischer Einsatz: eine Web-Anwendung (BFF) und mehrere KI-Agenten greifen gemeinsam auf denselben Dateibaum zu (z.B. eine Fallaktenverwaltung, eine Wissensdatenbank, ein Projektarchiv) — ohne dass sich Schreibvorgänge gegenseitig überschreiben und ohne dass Zugriffsrechte hart im jeweiligen Client verdrahtet werden müssen.

## 2. Geltungsbereich dieses Repos

**Die Agent-API — und generische, domänenunabhängige Zugriffs-Bridges darauf.** Die Trennlinie ist nicht „nur die Agent-API", sondern **„kein Fach-Repo, keine Domänenlogik"** (§1): domänengekoppelte Konsumenten (ein Web-Frontend, ein Orchestrator, eine mobile App) leben in ihren eigenen Projekten. Die **WebDAV-Bridge** (§14) hingegen hat **keine inhaltliche Kopplung an Fachdaten** — sie übersetzt ein generisches Protokoll (WebDAV) in generische Agent-API-Calls, genau wie die Agent-API selbst generisch ist. Deshalb lebt sie **in diesem Repo**, obwohl sie technisch ein weiterer BFF-Typ ist.

```
Domänengekoppelte Konsumenten          Generische Konsumenten (dieses Repo)
(eigene Projekte)                      ┌─────────────────────────┐
Web-Frontend, später                   │  WebDAV-Bridge (§14)     │
Mobile-App/Orchestrator/               │  Explorer/Netzlaufwerk   │
autonome Agenten                       └────────────┬────────────┘
        │                                            │
        │  Client-API-Key (+ optional X-User-Token, §10)
        ▼                                            ▼
                    Agent-API  (dieses Repo)
                              │
                              ▼
      Storage: Text im Git-Arbeitsbaum; Binärdateien vorerst ebenfalls in git (§7)
```

## 3. Datenmodell

- `kind`: `dir` | `md` | `json` | `pii.json` | `binary` | `pii.binary`
- Versions-Token je Datei: bei Text-Kinds Hash/Timestamp, bei `binary`/`pii.binary` der Hash der Bytes
- Storage-Backend: Git-Arbeitsbaum. Jede Transaktion erzeugt genau einen Commit (Audit, §9)

**PII-Erkennung generisch am Dateinamen, nicht kind-spezifisch hartkodiert:** `pii.json` war bisher der einzige Sonderfall (`path.endswith(".pii.json")`, `storage/git_repo.py::classify_kind`). Generalisiert auf eine einheitliche Regel: **jeder Dateiname, der das Infix `.pii.` enthält**, gilt als PII-klassifiziert — unabhängig von der eigentlichen Endung. `bericht.pii.json` → `pii.json` (wie bisher), `foto.pii.jpg` → `pii.binary` (neu), alles andere ohne `.pii.`-Infix → `json`/`md`/`binary` wie bisher. Grund: Bildinhalte lassen sich nicht feldscharf in PII/Nicht-PII trennen wie JSON-Felder — bei einem Foto mit erkennbaren Personen ist die *ganze Datei* PII, das muss sich also am `kind` der Datei selbst zeigen, nicht erst in einem Sidecar (§7.1). Nebeneffekt: ein zur Originaldatei gehöriges Sidecar wie `foto.pii.jpg.json` matcht automatisch mit — konsistent restriktiv, kein Sonderfall nötig.

Kind-Gate (§8) und PII-Audit-Flag (§9.2) gelten für `pii.binary` genau wie für `pii.json` — überall, wo aktuell `kind is PII_JSON` geprüft wird, gilt künftig `kind in {PII_JSON, PII_BINARY}` (`api/files.py`, `api/tree.py`, `api/transactions.py`).

> **Status: spezifiziert, noch nicht implementiert.** Umsetzung (Kind-Enum, `classify_kind`, alle `PII_JSON`-Vergleichsstellen, Tests) ist offener nächster Schritt.

## 4. Read-Pfad

```
GET /tree?root={path}&depth={n}&cursor={token}
```
- `root` (default `/`): nur diesen Teilbaum liefern
- `depth` (default 2): Ebenen unterhalb von `root` (`0` = nur Metadaten von `root`, `1` = nur direkte Kinder, …)
- `cursor`: Pagination pro Verzeichnisebene bei vielen Einträgen
- Antwort je Eintrag: `path`, `kind`, `size`, `version`, `preview` (nur Text-Kinds), `mime_type` (nur `binary`)
- **ACL-gefiltert:** Einträge ohne Read-Recht werden **komplett ausgeblendet**, nicht nur inhaltlich gesperrt (§10)

```
GET /file/{path}          Text: Volltext + Version · Binär: nur Metadaten (Version/Größe/mime_type)
GET /file/{path}/content  nur Binär: Rohbytes/Download (keine Text-Extraktion — bleibt außerhalb dieser API)
```

Aggressiv cachebar (`ETag`/`If-None-Match`) — Lesen ≫ Schreiben.

## 5. Write-Pfad

Jeder schreibende Call trägt Pflichtfelder: `user_id`, `client_id` (aus Auth, §10), `reason`, optional `task_id`.

**Text (`md`/`json`/`pii.json`):**

| Endpoint | Semantik |
|---|---|
| `POST /file/{path}/edit` | `old_str`/`new_str`/`if_version` — `old_str` muss **genau einmal** im aktuellen Inhalt vorkommen (0 oder >1 Treffer = Fehler). Doppelte Absicherung: Versions-Check **und** exakter String-Match |
| `POST /file/{path}/append` | kein `old_str` nötig, nur `if_version` |
| `POST /file/{path}` | Full Write — neue Datei oder große Restrukturierung |
| `DELETE /file/{path}` | nur mit `if_version`, immer explizit |

**Binär (`binary`, z.B. Word/PDF/Bilder):** kein `str_replace` — alt/neu ergibt keinen Sinn.

| Endpoint | Semantik |
|---|---|
| `POST /file/{path}` | Full Replace/Upload, `if_version` = Hash der Bytes |
| `DELETE /file/{path}` | mit `if_version` |

**Konfliktfall:** `409` liefert aktuellen Inhalt + aktuelle Version zurück (§11).

## 6. Transaktionen

**Single-Call-Transaktion** (Regelfall). `user_id`/`client_id` stehen **nicht** im Body — sie werden wie bei jedem anderen schreibenden Call serverseitig aus den Auth-Headern verifiziert (§10), nie aus Client-Angaben übernommen (sonst könnte ein Client sich als anderer Nutzer ausgeben):

```
POST /transaction
{ "task_id": "…", "reason": "…",
  "operations": [ {path, type, old_str, new_str, if_version}, … ] }
```

Zweiphasig: (1) alle Operationen gegen aktuelle Versionen/String-Matches/**ACL** validieren, nichts schreiben; (2) erst wenn **alle** valide sind, alle Dateien schreiben + **ein** Git-Commit über alle betroffenen Dateien.

Serialisiert über eine **globale, serielle Write-Queue** — bei überschaubarer Anzahl gleichzeitig aktiver Agenten mit blockweisem/burstigem Zugriffsmuster (z.B. 5 Zugriffe, dann 1–2h Pause) ausreichend performant, **keine** DB mit Row-Versioning nötig. Erst nachrüsten, wenn Warteschlangen-Zeiten das real belegen.

> **Architektur-Grenze: genau ein schreibender Prozess.** Das gesamte Locking-Modell dieser Spec (Write-Queue, OCC-Sicherheit zwischen Versions-Check und Schreiben, Read-Sperrung während der Schreibphase) gilt nur, solange **exakt ein** Prozess pro Instanz exklusiv auf den Git-Arbeitsbaum schreibt — siehe Locking-Implementierung unten. Das ist keine Performance-Optimierung, sondern eine **fundamentale Modellannahme**: Ein In-Process-Lock schützt nur innerhalb desselben Prozessspeichers. Sollte künftig mehr als ein schreibender Prozess auf denselben Baum nötig werden (Multi-Worker, horizontale Replicas, mehrere Instanzen gegen denselben Baum), reicht eine Konfigurationsänderung **nicht** — das erfordert einen echten Umbau des Locking-Mechanismus (z.B. verteiltes Lease/Lock über einen externen Koordinator, oder Rückkehr zu einer DB mit Row-Versioning, vgl. §6 oben). Bis dahin: Single-Writer-Prozess ist harte Voraussetzung, kein Implementierungsdetail.

**Locking-Implementierung:**
- Die Write-Queue ist eine echte **In-Process-kritische Sektion** (z.B. ein `asyncio.Lock` oder ein Single-Consumer-Worker), die Phase 1 (Validierung) + Phase 2 (Datei-Writes + Git-Commit) einer Transaktion vollständig umschließt — nicht nur den Schreibvorgang. Dadurch kann zwischen Versions-Check und Schreiben keine andere Transaktion die Version ändern; **keine Lockfiles pro Datei nötig.**
- Voraussetzung: **genau ein** Worker-Prozess pro Instanz (kein Multi-Worker/Multi-Replica auf demselben Git-Arbeitsbaum) — sonst ist die In-Process-Lock wirkungslos. Beim Start hart absichern (z.B. Abbruch, wenn mehr als ein Worker-Prozess konfiguriert ist), damit die Ein-Instanz-Garantie nicht still im Deployment bricht.
- **Reads während der Schreibphase:** `GET /tree`/`GET /file` werden während der (kurzen) Schreibphase einer Transaktion ebenfalls über dieselbe Lock kurz zurückgestellt, damit kein Leser einen Zwischenstand sieht (Datei 1 neu, Datei 2/3 noch alt). Passt zum erwarteten Zugriffsmuster (burstig, dann Pausen) — die Sperrzeit ist vernachlässigbar.
- **Spätere Option (bei Bedarf):** statt Reads zu sperren, Commits über Git-Plumbing atomar machen — neue Blobs schreiben, Tree-Objekt bauen, Commit-Objekt erzeugen, Branch-Ref erst am Ende atomar umbiegen, danach `checkout`. Leser sehen dann immer entweder den vollständigen alten oder vollständigen neuen Stand, ganz ohne Read-Lock. Mehr Implementierungsaufwand (GitPython Low-Level-API statt einfacher Datei-Writes) — erst umsetzen, wenn Lesesperren real zum Problem werden.

Multi-Call-Lifecycle (`begin`/`edit`/`commit`/`abort`, inkl. TTL+Reaper-Job) nicht für den Start — erst bei echtem Bedarf.

## 7. Binärdateien: Storage-Entscheidung (Phase 1)

Binärdateien bleiben **vorerst direkt in git**, trotz bekannter Nachteile (Repo-Wachstum, keine sinnvollen Diffs) — Content-Addressable Storage außerhalb von git wurde diskutiert und verworfen, weil das Risiko, Dateien im Notfall nicht direkt wiederzufinden, das Vertrauen in die Ablage senkt.

**Spätere Option** (bei zu großem Repo-Wachstum, z.B. Bilderordner): lokales CAS mit Hash-Sharding-Verzeichnissen + Manifest-Datei je Pfad in git, kein Drittanbieter nötig.

### 7.1 Metadaten zu Binärdateien (z.B. Bildern) — Sidecar-Konvention, kein neuer `kind`

Für Metadaten zu Binärdateien wird **kein** eigener `kind` und **kein** Sonderfeld in der Agent-API eingeführt — das würde Fachlogik (was ist ein "sinnvolles" Bild-Metadatum) in dieses domänenunabhängige Repo ziehen (§2). Stattdessen: reine Namenskonvention auf bestehendem Datenmodell.

- Zu `<pfad>/<datei>.<ext>` (`kind: binary`) liegt optional `<pfad>/<datei>.<ext>.meta.json` (`kind: json`) im selben Verzeichnis — eine ganz normale, unabhängig versionierte/ACL-geschützte JSON-Datei, kein API-Sonderfall.
- `GET /tree` listet beide Einträge nebeneinander; ein Agent liest/schreibt das Meta-JSON über die normalen `GET|POST /file/{path}`-Endpunkte.
- Das **Schema** des Meta-JSON definiert und pflegt der Konsument, nicht dieses Repo.
- **PII bei Binärdateien** (z.B. Foto mit erkennbaren Personen): nicht als Feld im Sidecar, sondern am **Dateinamen der Originaldatei** selbst (`.pii.`-Infix, z.B. `foto.pii.jpg`) — siehe §3 für die generische Erkennungsregel und die Begründung, warum das bei Bildinhalten kind-scharf statt feldscharf sein muss.

## 8. Zugriffsrechte — Zwei-Achsen-Identität × Pfad × Kind

Zwei unabhängige Identitäten je Request:
- **`user_id`** — wer ist verantwortlich (Mensch, z.B. `human:alice`, oder System-Account bei vollautonomen Agenten, z.B. `system:planner-03`)
- **`client_id`** — über welchen Kanal (`web-app`, `mobile-app`, `orchestrator`, …)

Grund: ein Client wird von mehreren Nutzern aufgerufen, und derselbe Nutzer soll über unterschiedliche Clients (Web vs. Mobile) unterschiedliche Sichtbarkeit haben können.

**ACL-Regeln je Scope**, kombiniert per Schnittmenge (beide müssen erlauben):
```json
[
  {"scope": "user", "user_id": "human:alice", "path_prefix": "/finance/", "read": "allow", "write": "allow"},
  {"scope": "client", "client_id": "mobile-app", "path_prefix": "/finance/", "read": "deny", "write": "deny"},
  {"scope": "client", "client_id": "web-app", "path_prefix": "/", "read": "allow", "write": "allow"}
]
```
- Effektive Regel für `(user, client, path)`: eine spezifische **Paar-Regel** (User+Client-Kombination) gewinnt, falls vorhanden; sonst die **restriktivste** Entscheidung aus User-, Client- und Global-Regeln (eine Regel ohne `user_id` *und* ohne `client_id` gilt für jeden Principal).
- **Default ohne passende Regel: fail-closed (`deny`)** — sobald überhaupt eine `_system/acl.json` im Baum existiert (auch mit `[]`-Inhalt), gilt für jede nicht abgedeckte Kombination aus `user_id`/`client_id`/Pfad/`kind` Deny, nicht Allow. Eine unvollständige oder fehlerhafte ACL-Konfiguration darf niemals unbekannten Principals versehentlich Zugriff geben. **Einzige Ausnahme:** existiert noch **gar keine** `acl.json` (frischer Baum, Bootstrap), bleibt es bei Default-Allow — sonst könnte nicht einmal der Admin die allererste ACL-Datei schreiben, weil auch dieser Schreibzugriff durch dieselbe Prüfung läuft (`/_system/acl.json` ist zusätzlich hart auf den Admin-`user_id` beschränkt, §10a, aber eben zusätzlich zur normalen ACL-Prüfung, nicht statt ihr).
- `kind`-Regel greift **zusätzlich und unabhängig vom Pfad** (z.B. generelles Schreibverbot für alle `pii.json`, unabhängig vom Ort) — additiv UND-verknüpft mit der Scope-Entscheidung, `deny` gewinnt. Gilt gleichermaßen für `pii.binary` (§3).
- Erste zutreffende, **spezifischere** Regel gewinnt (Pfad-Präfix vor allgemeinem `/`).
- Durchgesetzt bei `GET /tree` (Einträge ausblenden) **und** bei jedem einzelnen Read/Write-Call.

**Glob-Muster in `path_prefix`:** Ein Muster ohne `*` bleibt ein reiner Literal-Präfix wie oben. Enthält es `*`, wird stattdessen als Glob gegen den **vollen Pfad** gematcht (nicht nur als Präfix) — `*` matcht beliebige Zeichen außer `/` (bleibt im Pfadsegment), `**` matcht auch über `/` hinweg. Damit lässt sich ein ganzes Dokument über alle `kind`-Varianten hinweg sperren und gezielt wieder öffnen:

```json
[
  {"path_prefix": "lease*.*", "write": "deny"},
  {"path_prefix": "lease.json", "write": "allow"}
]
```

`lease.json` ist schreibbar, `lease.pii.json` und `lease.md` bleiben gesperrt — die spezifischere Regel (mehr Literal-Zeichen, siehe `_specificity_score` in `acl/engine.py`) gewinnt nur für den Pfad, den sie exakt trifft. Ein Glob ist bewusst ein **Voll-Match**, kein Präfix-Match: `lease*.md` matcht `lease.md`, aber nicht `lease.md.bak`.

**Orchestrator-Fall:** authentifiziert sich selbst als eigener `client_id`, reicht aber den `user_id`-Claim des auslösenden Menschen durch die ganze Kette weiter (Frontend → Orchestrator → Agent-API) — jeder Call bekommt die Rechte des tatsächlich auslösenden Nutzers, nie pauschal die des Orchestrators.

**ACL-Verwaltung:** versionierte JSON-Datei im selben Tree (`/_system/acl.json`), über dieselbe API verwaltet — Write nur für einen fest definierten Admin-`user_id`, unabhängig von den sonstigen Pfad/Kind-Regeln. Kein separater Config-Service.

## 9. Audit

### 9.1 Schreibvorgänge — Git-Commit

- **Ein Commit pro Transaktion**, keine zeitbasierte Bündelung.
- `reason` ist Pflichtparameter, vom auslösenden Client geliefert. Server reichert automatisch an: `user_id`, `client_id`, Task-ID, Timestamp, Session-ID, betroffene Dateien.
- Kein nachträgliches Generieren von `reason` per LLM aus dem Diff.
- Optional: Tages-Snapshots/Tags für groben Überblick, ohne granulare Commits zu opfern.

### 9.2 Lesevorgänge — ebenfalls vollständig protokolliert

Nicht nur Schreib-, auch **jeder lesende Call** (`GET /tree`, `GET /file/{path}`, `GET /file/{path}/content`) wird protokolliert. Besonders markiert: jeder Zugriff — lesend **oder** schreibend — auf eine `kind: pii.json`-Datei setzt `pii_accessed: true` (§9.3), damit sich „wer hat wann welche personenbezogenen Daten gesehen" jederzeit beantworten lässt.

**Warum Reads nicht über Git-Commits laufen können:** Ein Commit ist für eine Content-Änderung gedacht — ein Read verändert nichts, es gäbe keinen sinnvollen Commit-Inhalt. Bei „Lesen ≫ Schreiben" (§4) würde reines Lese-Rauschen die Commit-Historie zudem unbrauchbar für ihren eigentlichen Zweck (Content-Historie) machen. Reads brauchen zwingend einen **eigenen** Speicher — siehe §9.3.

### 9.3 Audit-Log-Speicherung — konfigurierbarer Port

Kein festverdrahteter Speicher, sondern ein **Port mit austauschbaren Adaptern** (analog zu anderen Extraktions-/Speicher-Ports: das System kennt nur die Schnittstelle, nicht das Verfahren). Der Rest der Agent-API kennt nur `AuditSink.record(entry) -> None` (und optional `.query(filters) -> AuditLogResponse` — nicht jeder Adapter unterstützt Abfragen, s.u.), nicht das konkrete Backend.

**Konfiguration:** Env-Var `AGENT_API_AUDIT_BACKEND` = `none | sqlite | jsonl | loki | elk | azure | aws`.

| Backend | Status | Funktionsweise |
|---|---|---|
| `none` | **gebaut** | No-Op — kein Overhead, aber auch kein Compliance-Nutzen. Nur für Dev/Test gedacht, nicht für den Produktivbetrieb. |
| `sqlite` | **gebaut** | Eigene SQLite-Datei, WAL-Modus, append-only. **Einziger Adapter mit echter `query()`-Unterstützung** — speist `GET /system/audit` (§9.4) direkt. |
| `jsonl` | **gebaut** | Strukturierte JSON-Lines nach stdout/Datei, **kein eigener Netzwerk-Client**. Deckt Loki/ELK/Azure/AWS ab, indem ein **externer Standard-Shipper** die Zeilen abholt: Promtail → Loki, Filebeat/Logstash → ELK, Azure Monitor Agent → Log Analytics, CloudWatch Agent → CloudWatch Logs. `query()` **nicht unterstützt** — Auswertung läuft im jeweiligen externen Tool (Grafana/Kibana/Azure Monitor/CloudWatch Insights), nicht über `GET /system/audit`. |
| `loki`/`elk`/`azure`/`aws` als **native SDK-Push-Adapter** | **nicht gebaut** | Nur falls `jsonl` + Shipper-Agent nicht reicht — direkter Push aus der App heraus, ohne Zwischenschritt. |

**Kein Widerspruch** zur „keine DB nötig"-Entscheidung in §6 — die betraf Content-Versionierung/OCC (Read-Modify-Write mit Konfliktlogik). Audit-Protokollierung ist bei jedem Adapter rein additiv (nur Anhängen, keine Konflikte), ein anderes Problem.

**Eintrag je Call** (backend-unabhängiges Schema): `id`, `timestamp`, `user_id`, `client_id`, `operation` (`tree`|`read`|`read_content`|`edit`|`append`|`write`|`delete`|`transaction`), `path` (bzw. Pfad-Liste bei `transaction`), `kind`, `pii_accessed` (bool), `commit_id` (nullable, nur bei Writes — Verweis auf den Git-Commit für Diff-Details), `reason` (nullable, nur bei Writes), `result` (`success`|`denied`|`error`), `task_id` (nullable).

- **Ort (bei `sqlite`/`jsonl`):** eigener Pfad außerhalb des Git-Arbeitsbaums (`AGENT_API_AUDIT_DB_PATH` bzw. `AGENT_API_AUDIT_LOG_PATH`, siehe [deployment.md](deployment.md)) — Audit-Daten gehören nicht in den versionierten Inhalt, sonst würde jeder Lese-Log-Eintrag selbst wieder eine Content-Änderung (und damit einen weiteren zu protokollierenden Read) auslösen.
- **Append-only, unveränderlich** — keine UPDATE/DELETE, Korrekturen (falls je nötig) als neuer Eintrag.
- **Synchron geschrieben**, vor der Response — bei diesem Volumen kein spürbarer Latenz-Effekt; sonst Risiko, dass ein tatsächlich erfolgter Read nie protokolliert wird (bei Compliance-Zweck kein akzeptables Risiko).
- **Middleware statt Einzelaufrufe:** ein FastAPI-Middleware-Layer ruft `AuditSink.record()` nach Abschluss jedes Requests (inkl. Ergebnis-Status) genau einmal auf — verhindert vergessene Log-Aufrufe in einzelnen Endpoint-Handlern, und macht den Adapter-Wechsel zur reinen Konfigurationsfrage.
- **Volumen/Retention** (unverbindlich, spätere Verfeinerung): bei sehr häufigem `GET /tree`-Polling (z.B. durch die WebDAV-Bridge, §14) kann das Log groß werden — unkritisch für SQLite/JSONL an sich, aber eine Retention-/Archivierungsregel ist irgendwann sinnvoll.

### 9.4 Abfrage

`GET /system/audit` (Admin-only, siehe [openapi.yaml](openapi.yaml)) mit Filtern `user_id`, `path_prefix`, `pii_only` (bool), `from`/`to`, `operation` — deckt den Compliance-Fall „zeig mir alle PII-Zugriffe von X im Zeitraum Y" direkt ab, **sofern der konfigurierte Adapter `query()` unterstützt** (heute: nur `sqlite`). Bei `jsonl`/`none`/den späteren Cloud-Adaptern liefert der Endpoint einen klaren Hinweis („Abfrage nicht unterstützt bei Backend X, Auswertung über <externes Tool>") statt eines leeren oder irreführenden Ergebnisses.

## 10. Authentifizierung

Zwei getrennte Mechanismen — ein API-Key allein beweist nur den Client, nicht den Menschen dahinter:

**a) Client-Authentifizierung (API-Key + Secret)**
Jeder registrierte Client bekommt einen eigenen API-Key (Client-Registry der Agent-API, Key gehasht gespeichert, rotier-/widerrufbar). `Authorization: Bearer <client_api_key>` — Agent-API verifiziert daraus den echten `client_id`, nicht aus dem Body übernommen.

**b) User-Identität (signierter Kurzzeit-Token)**
Nach Human-Login stellt der jeweilige Client (z.B. das Web-Frontend) einen kurzlebigen, signierten Token aus (`user_id`, `client_id`, `exp` ~15 min, EdDSA/Ed25519 mit privatem Schlüssel des Clients). Header `X-User-Token: <JWT>`. Agent-API verifiziert die Signatur mit dem **Public Key aus der Registry** (nicht aus dem Token selbst — sonst könnte sich ein Client als anderer ausgeben), prüft `iss == verifizierter client_id`, `exp`, Key nicht `revoked`.

Autonome Agenten ohne Menschen dahinter: eigener API-Key, direkt gemappt auf eine System-`user_id` (`fixed_user_id` in der Registry) — kein Delegationsschritt, kein Token nötig.

| Ebene | Mechanismus | Beweist |
|---|---|---|
| Client (Service) | API-Key + Secret, lokal verwaltet | Anfrage kommt wirklich vom angegebenen Client |
| User (Mensch) | Signierter Kurzzeit-Token nach Login | Aufruf ist wirklich für den angegebenen Nutzer |
| Autonome Agenten ohne Mensch | Eigener API-Key = direkte Identität | kein Delegationsschritt nötig |

Token-Erneuerung ist Sache des jeweiligen Clients (Session-/Refresh-Handling außerhalb des Scopes der Agent-API).

## 11. Konflikt-Eskalation

- `409` liefert aktuellen Inhalt + aktuelle Version zurück, damit der Agent ohne erneuten `GET` neu planen kann.
- **Automatischer Retry:** bis zu **3** Versuche, jeweils mit frischer Version + neu berechnetem Patch.
- **Nach 3 gescheiterten Versuchen:** Eskalation als Konfliktdatei im Tree unter `/_conflicts/{original_path}.conflict.{timestamp}.json`:
  ```json
  {
    "conflict_id": "cf_8a2f",
    "original_path": "/notes/pricing.md",
    "user_id": "human:alice",
    "client_id": "web-app",
    "task_id": "pricing-update-42",
    "reason": "Preis aktualisiert",
    "attempted_version": "v9",
    "attempted_old_str": "Preis: TBD",
    "attempted_new_str": "Preis: 4.200€",
    "current_content_at_escalation": "...",
    "retry_count": 3,
    "created_at": "2026-08-04T10:15:00Z",
    "status": "open"
  }
  ```
- **Täglicher Cleanup-Job:** räumt aufgelöste/veraltete Konfliktdateien auf; **benachrichtigt aktiv** (Slack/E-Mail) bei neu aufgetretenen, noch ungelösten Konflikten seit dem letzten Lauf.
- Kein Server-seitiges Auto-Merge. Manuelle Auflösung über das UI des jeweiligen Clients (außerhalb dieses Repos) — Mensch entscheidet: erneut anwenden, verwerfen, manuell mergen.

## 12. Design-Entscheidungen im Überblick

- Repo enthält die Agent-API sowie generische, domänenunabhängige Bridges darauf (§2); domänengekoppelte Frontends leben in ihren eigenen Projekten.
- Tree-Read mit Pagination (`root`/`depth`/`cursor`), Binärdateien-Handling, Zwei-Achsen-ACL (User×Client×Pfad×Kind), Client+User-Auth, Konflikt-Eskalation mit Cleanup-Job.
- Binärdateien bleiben Phase 1 in git (§7), CAS als spätere Option festgehalten.
- Tech-Stack: Python + FastAPI + GitPython + `cryptography`/PyJWT für EdDSA.
- **Multi-Projekt-Betrieb:** **eine Instanz pro Projekt/Baum**, separates Deployment je Konsument — kein multi-tenantes Modell. Das ACL-/Registry-Modell (§8/§10) bleibt entsprechend auf **einen** Baum je Instanz ausgelegt.
- `user_id`/`client_id` kommen ausschließlich aus den verifizierten Auth-Headern (§10), nie aus dem Request-Body (sonst Identitäts-Spoofing möglich) — eine frühe Designfassung hatte das an einer Stelle inkonsistent.
- Deployment-Form: isoliert gebaut, aber als **eigener Prozess/Container im selben Deployment-Bund** wie das konsumierende Frontend (Sidecar, über HTTP mit Client-API-Key/JWT angesprochen) — **nicht** als in-process Bibliothek importiert, damit die serielle Write-Queue/OCC (§6/§8) bei mehreren Frontend-Workern/Replicas ihre Ein-Instanz-Garantie behält.
- Lesevorgänge werden auditiert, PII-Zugriffe gesondert markiert (§9.2).
- Audit-Log als konfigurierbarer Port (§9.3): `none|sqlite|jsonl|loki|elk|azure|aws`, gebaut: `none`+`sqlite`+`jsonl` (Letzteres deckt Loki/ELK/Azure/AWS über externe Standard-Shipper ab). Native SDK-Push-Adapter für Loki/ELK/Azure/AWS vorerst nicht geplant.
- WebDAV-Bridge (§14): vollständiges Design (Architektur, Auth-Übersetzung, Protokoll-Mapping, Tech-Stack-Vorschlag `wsgidav`). Bleibt in diesem Repo (generisch, keine Domänenkopplung).
- Glob-Muster in `path_prefix` (§8): `*`/`**` unterstützt, erlaubt z.B. ein Dokument über alle `kind`-Varianten hinweg zu sperren und einzelne Kinds gezielt wieder zu öffnen. Feldname bewusst unverändert (kein Schema-Bruch) — Muster ohne `*` bleiben reine Literal-Präfixe.
- Metadaten zu Binärdateien (§7.1): Sidecar-Konvention (`<datei>.<ext>.meta.json`), kein neuer `kind`, kein Schema-Wissen in diesem Repo.
- `pii.binary`-Kind + generische `.pii.`-Infix-Erkennung (§3/§7.1/§8): spezifiziert, **noch nicht implementiert** — `classify_kind`, `Kind`-Enum, alle `PII_JSON`-Vergleichsstellen müssen nachgezogen werden.

## 13. Backlog (unverbindlich)

Vier mögliche spätere Erweiterungen, keine davon blockiert den aktuellen Stand — angeregt durch den Vergleich mit ähnlichen Multi-Agent-Koordinationsprojekten:

1. **Foreign-Edit Guards** — erkennen, ob jemand am Git-Arbeitsbaum vorbei direkt geschrieben hat.
2. **Snapshot-Sessions / Read-Skew-Schutz** — für Multi-File-Reads über `GET /tree`, bislang nur für Writes (§6) adressiert.
3. **Invalidierung statt Re-Fetch** — kleines Signal statt vollem Re-Fetch bei häufigem Polling.
4. **Effect-Ordering-Gate-Pattern** (Decide → Re-Check vor Effect → Fire/Hold) für Fälle, in denen ein Agent aus einem Read eine später wirksame Entscheidung ableitet.

## 14. WebDAV-Bridge

Menschen greifen weiterhin **dateibasiert über den Explorer/Finder** zu (WebDAV-Laufwerk), statt zwingend über eine Web-UI. Architektonisch ein weiterer Client-Typ, bleibt aber **in diesem Repo** (§2) — keine inhaltliche Kopplung an Fachdaten, rein generische Protokollübersetzung.

### 14.1 Architektur

```
Arbeitsplatzrechner                     Server (dieses Repo)
Explorer/Finder ──WebDAV/HTTPS──> Reverse Proxy (TLS) ──> WebDAV-Bridge ──> Agent-API
```

**Wichtiger Unterschied zum Sidecar-Muster (§12):** Anders als Frontend↔Agent-API (typischerweise auf demselben Host, nur `127.0.0.1`) muss die WebDAV-Bridge **von außerhalb des Servers erreichbar** sein — sie wird vom Client-PC gemountet, nicht von einem Prozess auf demselben Host. Also: eigener öffentlicher (oder VPN-beschränkter) HTTPS-Endpoint mit TLS (Reverse Proxy, Let's Encrypt) — hier greift kein „nur intern erreichbar"-Sicherheitsnetz, die Basic-Auth-Absicherung (§14.2) trägt die volle Last.

### 14.2 Auth-Übersetzung — Bridge handhabt die Header selbst

Der eingebaute WebDAV-Client von Windows/macOS kann **nur HTTP Basic Auth** senden, keine custom Header. Die Bridge übernimmt die Übersetzung vollständig, transparent für den Client:

1. Bridge ist selbst ein registrierter Agent-API-Client (`client_id: webdav-bridge`, `issues_user_tokens: true`, eigenes Signing-Keypair, §10).
2. Bridge führt eine **eigene, kleine Zugangsverwaltung** (Benutzername → `user_id` + gehashtes Personal-Access-Token als „Passwort") — admin-only, unabhängig von der Agent-API-Client-Registry, da es eine reine Mensch-Zugangsebene *vor* der Bridge ist.
3. Bei jedem eingehenden WebDAV-Request: Basic-Auth-Credentials prüfen → `user_id` auflösen → User-Token minten (kurzlebig, ggf. für die Dauer der Session gecacht) → ausgehenden Agent-API-Call mit `Authorization: Bearer <bridge_client_api_key>` + `X-User-Token: <JWT>` absetzen.

Aus Sicht des Explorer/Finder ist es ein normales WebDAV-Laufwerk mit Benutzername/Passwort.

### 14.3 Protokoll-Mapping

| WebDAV | Agent-API |
|---|---|
| `PROPFIND` (Verzeichnis) | `GET /tree?root=…&depth=1` |
| `PROPFIND` (Datei) | `GET /file/{path}` (Metadaten) |
| `GET` | `GET /file/{path}` (Text) bzw. `GET /file/{path}/content` (Binär) |
| `PUT` | `POST /file/{path}` — `if_version` aus einem von der Bridge selbst vorgezogenen `GET` (der Client liefert keine Version) |
| `DELETE` | `DELETE /file/{path}` mit zuvor ermitteltem `if_version` |
| `MKCOL` | kein Agent-API-Äquivalent nötig — Verzeichnisse entstehen implizit über Dateipfade; Bridge beantwortet `MKCOL` als Erfolg ohne eigene Aktion |
| `MOVE`/`COPY` | **atomar über `POST /transaction`**: Bridge liest Inhalt+Version der Quelle, sendet eine Transaktion mit `write` (Zielpfad, Inhalt) + bei `MOVE` zusätzlich `delete` (Quellpfad, `if_version`) — beide Operationen in einem Commit (§6), kein Sonderfall nötig. **Einschränkung:** `/transaction` kennt bisher nur Text-`content` (String) — bei `kind: binary` daher (vorerst) **nicht atomar**: drei separate Calls (Metadaten+Bytes lesen, `POST` auf Zielpfad, `DELETE` auf Quellpfad). Bewusst offen dokumentiert, nicht stillschweigend generalisiert — Erweiterung von `/transaction` um Binär-Support (`content_base64`) als Backlog-Punkt, falls das in der Praxis stört. |
| `LOCK`/`UNLOCK` | nicht unterstützt (No-Op-Ablehnung) — OCC bleibt das einzige Sicherheitsnetz (§11), bis sich ein echter Bedarf für Locking zeigt |

### 14.4 `reason`-Feld

Automatisch generiert (z.B. `"Bearbeitet über WebDAV"`), da der Explorer/Finder keine Eingabemöglichkeit dafür hat — akzeptierter Qualitätsverlust bei diesem Zugriffsweg, wie bei jedem automatisiert erzeugten Reason in dieser Spec nicht per LLM nachträglich „aufgehübscht" (§9.1).

### 14.5 Konfliktverhalten

`409` wird an den Client durchgereicht (generischer Fehler, keine Merge-UI) — OCC bleibt **strikt durchgesetzt**, kein stilles Last-Write-Wins. Die Konfliktdatei entsteht wie bei jedem anderen Client (§11), unabhängig vom auslösenden Zugriffsweg — Auflösung kann bei Bedarf über eine komfortablere Web-UI erfolgen, WebDAV ist nicht der einzige Zugriffsweg.

### 14.6 Tech-Stack

`wsgidav` (Python, ausgereifte WebDAV-Serverbibliothek, pluggable `DAVProvider`) statt eigener WebDAV/XML-Protokollimplementierung — ein custom `DAVProvider` übersetzt in Agent-API-Calls (`httpx`). Reduziert die Bridge auf reine Übersetzungslogik statt Protokoll-Fummelkram.
