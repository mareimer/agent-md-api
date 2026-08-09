# Example: `STRUCTURE.md`

This is a template, not a real deployment. It shows the pattern described in [spec.md](spec.md) §3: a `STRUCTURE.md` file at the root of a tree served by `agent-md-api`, written for a hypothetical property-management deployment. Copy the idea, not the content — the actual directories, kinds, and naming rules are entirely up to whatever domain you're deploying this for.

Everything below this line is what would actually live in `STRUCTURE.md` inside such a tree — plain text, read by agents via the ordinary `GET /file/STRUCTURE.md`, not parsed or enforced by the Agent API itself.

---

# Directory structure

This tree holds one directory per property, plus a few cross-cutting top-level directories. Agents creating or filing new files should follow this layout — nothing in the API enforces it, but staying consistent keeps files discoverable for other agents and humans.

## Top level

```
/
├── properties/<property-id>/     one directory per property, see below
├── contacts/                     tenants, owners, contractors — not tied to one property
├── templates/                    reusable document templates (leases, notices)
└── STRUCTURE.md                  this file
```

`<property-id>` is a short, stable slug (e.g. `elmstreet-12`), not a display name — rename the display name in the property's own `info.json`, not the directory.

## Inside a property directory

```
properties/elmstreet-12/
├── info.json                     kind: json — address, unit count, purchase date, etc.
├── deed.pdf                      kind: binary — the source document
├── deed.skill                    kind: skill — how to extract deed.json from deed.pdf
├── deed.json                     kind: json — structured extract (owner, parcel number, encumbrances)
├── tenants/
│   └── <tenant-id>/
│       ├── lease.pdf             kind: binary
│       ├── lease.skill           kind: skill
│       ├── lease.json            kind: json — rent, term, deposit
│       └── lease.pii.json        kind: pii.json — tenant name, date of birth, ID number
├── insurance/
│   ├── policy.pdf                kind: binary
│   ├── policy.skill               kind: skill — insurance contracts are individually worded;
│   │                               the skill documents which fixed fields to still pull out
│   │                               reliably (policy number, premium, coverage type) even
│   │                               though full-document extraction isn't realistic
│   └── policy.json               kind: json
└── notes.md                      kind: md — free-form, no fixed structure expected
```

## Naming conventions

- One file family per source document: same base name, one file per `kind` (`kind: binary` for the source, `kind: skill` for extraction instructions, `kind: json`/`pii.json` for the result) — see spec.md §3.
- Personally identifiable data always goes in a `.pii.json` sidecar, never mixed into the plain `.json` — that's what the ACL kind-gate (spec.md §8) restricts on.
- `<tenant-id>`/`<property-id>` are slugs assigned once at creation, referenced from `info.json`/`lease.json` elsewhere — don't reuse a slug for a different entity even after move-out/sale.
