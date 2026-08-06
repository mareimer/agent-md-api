"""ACL-Auswertung (spec.md §8).

Zwei getrennte Auswertungsschritte, dann UND-verknüpft (deny gewinnt) — das
ist eine Synthese aus zwei Sätzen der Spec, die beide je für sich klar sind,
aber im Zusammenspiel nicht auf Byte-Ebene spezifiziert wurden. Hier
dokumentiert, damit die Auslegung nachvollziehbar bleibt:

`path_prefix` ist trotz des Namens kein reiner Präfix mehr: enthält das Muster
`*`, wird es als Glob gegen den vollen Pfad gematcht (Details im Docstring von
`AclRule` in domain/models.py). Die „spezifischer gewinnt"-Logik unten
funktioniert für beide Fälle gleich, über `_specificity_score` (Literal-
Zeichenzahl statt reiner String-Länge).

1. **Pfad-/Scope-Entscheidung** (`_scope_decision`) — vier Töpfe:
   - **Paar-Topf** (`user_id` UND `client_id` gesetzt) gewinnt, falls
     vorhanden und zutreffend, unter mehreren Paar-Regeln die mit dem
     spezifischeren `path_prefix` ("Pfad-Präfix vor allgemeinem /", spec.md §8).
   - Sonst: **restriktivste Entscheidung aus User-, Client- und
     Global-Topf** (spec.md §8 formuliert nur „restriktivste Entscheidung
     aus User- und Client-Regeln" — um den **Global-Topf** erweitert:
     eine Regel ohne `user_id` UND ohne `client_id`, z.B. nur
     `{"path_prefix": "finanzen/", "write": "deny"}`, gilt für **jeden**
     Principal, nicht für niemanden — sonst würde sie beim Einsortieren
     in keinen der ursprünglich zwei Töpfe passen und wirkungslos
     verpuffen). Je Topf wird zunächst die spezifischste eigene Regel
     ermittelt (längstes `path_prefix`), dann gilt: sagt irgendein Topf
     „deny", ist das Ergebnis „deny" — Deny-Dominanz ist gewollt, auch
     wenn ein anderer Topf explizit allow sagt.
   - Kein Topf hat eine passende Regel → **kein Scope-Veto** (nicht
     automatisch allow — siehe Schritt 3).
2. **Kind-Gate** (`_kind_gate_decision`) — Regeln mit gesetztem `kind`,
   unabhängig von `user_id`/`client_id`: „greift zusätzlich und unabhängig
   vom Pfad" (spec.md §8). Die spezifischste zutreffende Kind-Regel liefert
   ihre eigene Entscheidung, additiv zur Scope-Entscheidung.
3. **Kombination:** `deny`, wenn Scope-Entscheidung ODER Kind-Gate „deny"
   sagt. Sagt keines der beiden etwas (kein Treffer), **Default: allow**
   (spec.md §8).

Beispiel aus der Spec (§8), das dieses Modell korrekt abbildet: eine
Blanket-Regel `client=web-bff, path=/, allow/allow` (Scope-Entscheidung:
allow) plus `kind=pii.json, write=deny` (Kind-Gate: deny) → Ergebnis beim
Schreiben von `.pii.json`: **deny**, obwohl die Scope-Regel allow sagt.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache

from agent_md_api.domain.errors import AclDeniedError, NotFoundError
from agent_md_api.domain.models import AclRule, Kind, Permission, Principal

ACL_PATH = "_system/acl.json"
"""Versionierte ACL-Datei im Tree selbst (spec.md §8) — Schreiben nur für den Admin-user_id,
durchgesetzt an der API-Schicht (api/files.py), nicht hier in der reinen Auswertungslogik."""


def load_acl_rules(storage) -> list[AclRule]:  # noqa: ANN001 - vermeidet Zirkelimport auf GitStorage
    """Fehlt die Datei (frischer Baum, noch keine ACL gepflegt): leere Regelliste
    -> Default-Allow für alles (spec.md §8), bewusstes Bootstrap-Verhalten."""
    try:
        file = storage.get_file(ACL_PATH)
    except NotFoundError:
        return []
    return [AclRule.model_validate(r) for r in json.loads(file.content or "[]")]


@lru_cache(maxsize=256)
def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """`*` matcht alles außer `/` (bleibt im Pfadsegment), `**` matcht auch über
    `/` hinweg. Kein Fremdpaket nötig — die Grammatik ist klein genug für eine
    eigene, direkt testbare Übersetzung nach `re` (domain_models.py-Docstring)."""
    parts: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern[i : i + 2] == "**":
            parts.append(".*")
            i += 2
        elif pattern[i] == "*":
            parts.append("[^/]*")
            i += 1
        else:
            parts.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(parts) + "$")


def _path_matches(pattern: str | None, path: str) -> bool:
    if pattern is None:
        return True
    normalized = pattern.lstrip("/")
    if "*" not in normalized:
        return path.startswith(normalized)
    return _glob_to_regex(normalized).match(path) is not None


def _specificity_score(pattern: str | None) -> int:
    """Anzahl Literal-Zeichen (ohne `*`) — bei präfixartigen Mustern (kein `*`)
    identisch zu `len(pattern)`, also unverändertes Verhalten für alle bisherigen
    Regeln. Ein Glob mit mehr Literal-Anteil gilt als spezifischer als einer mit
    weniger, `**` zählt (bewusst) schwächer als ein einzelnes `*` gleicher Länge."""
    if not pattern:
        return 0
    return len(pattern.replace("*", ""))


def _matches(rule: AclRule, *, principal: Principal, path: str, kind: Kind) -> bool:
    if rule.user_id is not None and rule.user_id != principal.user_id:
        return False
    if rule.client_id is not None and rule.client_id != principal.client_id:
        return False
    if not _path_matches(rule.path_prefix, path):
        return False
    if rule.kind is not None and rule.kind != kind:
        return False
    return True


def _most_specific(rules: list[AclRule]) -> AclRule | None:
    if not rules:
        return None
    return max(rules, key=lambda r: _specificity_score(r.path_prefix))


class AclEngine:
    def __init__(self, rules: list[AclRule]) -> None:
        self._rules = rules

    def _scope_decision(self, *, principal: Principal, path: str, kind: Kind, field: str) -> Permission | None:
        matching = [r for r in self._rules if getattr(r, field) is not None and _matches(r, principal=principal, path=path, kind=kind)]

        pair_rules = [r for r in matching if r.user_id is not None and r.client_id is not None]
        best_pair = _most_specific(pair_rules)
        if best_pair is not None:
            return getattr(best_pair, field)

        user_rules = [r for r in matching if r.user_id is not None and r.client_id is None]
        client_rules = [r for r in matching if r.client_id is not None and r.user_id is None]
        global_rules = [r for r in matching if r.user_id is None and r.client_id is None]
        user_decision = getattr(_most_specific(user_rules), field) if user_rules else None
        client_decision = getattr(_most_specific(client_rules), field) if client_rules else None
        global_decision = getattr(_most_specific(global_rules), field) if global_rules else None

        decisions = [d for d in (user_decision, client_decision, global_decision) if d is not None]
        if not decisions:
            return None
        if Permission.DENY in decisions:
            return Permission.DENY
        return Permission.ALLOW

    def _kind_gate_decision(self, *, principal: Principal, path: str, kind: Kind, field: str) -> Permission | None:
        matching = [
            r for r in self._rules
            if r.kind is not None and getattr(r, field) is not None and _matches(r, principal=principal, path=path, kind=kind)
        ]
        best = _most_specific(matching)
        return getattr(best, field) if best else None

    def _effective(self, *, principal: Principal, path: str, kind: Kind, field: str) -> Permission:
        scope = self._scope_decision(principal=principal, path=path, kind=kind, field=field)
        gate = self._kind_gate_decision(principal=principal, path=path, kind=kind, field=field)
        if scope is Permission.DENY or gate is Permission.DENY:
            return Permission.DENY
        if scope is Permission.ALLOW or gate is Permission.ALLOW:
            return Permission.ALLOW
        return Permission.ALLOW  # Default ohne jede passende Regel (spec.md §8)

    def can_read(self, *, principal: Principal, path: str, kind: Kind) -> bool:
        return self._effective(principal=principal, path=path, kind=kind, field="read") is Permission.ALLOW

    def can_write(self, *, principal: Principal, path: str, kind: Kind) -> bool:
        return self._effective(principal=principal, path=path, kind=kind, field="write") is Permission.ALLOW

    def require_read(self, *, principal: Principal, path: str, kind: Kind) -> None:
        if not self.can_read(principal=principal, path=path, kind=kind):
            raise AclDeniedError(f"/{path}: kein Leserecht für {principal.user_id}/{principal.client_id}.")

    def require_write(self, *, principal: Principal, path: str, kind: Kind) -> None:
        if not self.can_write(principal=principal, path=path, kind=kind):
            raise AclDeniedError(f"/{path}: kein Schreibrecht für {principal.user_id}/{principal.client_id}.")
