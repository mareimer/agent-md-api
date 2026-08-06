"""ACL-Engine (spec.md §8) — Zwei-Achsen-Regeln (User×Client×Pfad×Kind), Spezifitäts-
und Vorrangsreihenfolge aus acl/engine.py.

Wichtig für dieses Modul: `AclRule`-Regeln ohne `user_id` UND ohne `client_id` (nur
`path_prefix`, sonst nichts) landen im eigenen "Global-Topf" von `_scope_decision()`
und gelten als Blanket-Regel für JEDEN Principal (geklärt mit Alice, 2026-08-06) —
sie fließen in die restriktivste-Entscheidung genauso ein wie Pair-/User-/Client-Topf.
`kind`-Regeln bleiben zusätzlich und unabhängig davon als eigenes Kind-Gate (spec.md
§8: „greift zusätzlich und unabhängig vom Pfad")."""

from __future__ import annotations

from agent_md_api.acl.engine import AclEngine
from agent_md_api.domain.models import AclRule, Kind, Permission, Principal

ALICE_WEB = Principal(user_id="human:alice", client_id="web-bff")
ALICE_MOBILE = Principal(user_id="human:alice", client_id="mobile-bff")
OTHER_WEB = Principal(user_id="human:anna", client_id="web-bff")


def test_default_allow_without_any_matching_rule() -> None:
    engine = AclEngine([])

    assert engine.can_read(principal=ALICE_WEB, path="irgendwas.md", kind=Kind.MD) is True
    assert engine.can_write(principal=ALICE_WEB, path="irgendwas.md", kind=Kind.MD) is True


def test_pair_rule_wins_over_single_scope_rules() -> None:
    """spec.md §8 Beispiel: User-Regel erlaubt, Client-Regel verbietet, Paar-Regel
    (beide user_id UND client_id gesetzt) gewinnt."""
    rules = [
        AclRule(user_id="human:alice", path_prefix="finanzen/", read="allow", write="allow"),
        AclRule(client_id="mobile-bff", path_prefix="finanzen/", read="deny", write="deny"),
        AclRule(user_id="human:alice", client_id="mobile-bff", path_prefix="finanzen/", read="allow", write="deny"),
    ]
    engine = AclEngine(rules)

    assert engine.can_read(principal=ALICE_MOBILE, path="finanzen/uebersicht.md", kind=Kind.MD) is True
    assert engine.can_write(principal=ALICE_MOBILE, path="finanzen/uebersicht.md", kind=Kind.MD) is False


def test_kind_rule_applies_independent_of_path_and_of_user_client() -> None:
    """spec.md §8: generelles Schreibverbot für alle pii.json, unabhängig vom Ort —
    UND unabhängig von user_id/client_id (kind-Regeln sind hier bewusst scope-frei)."""
    rules = [AclRule(kind=Kind.PII_JSON, write="deny")]
    engine = AclEngine(rules)

    assert engine.can_write(principal=ALICE_WEB, path="irgendwo/tief/verschachtelt/person.pii.json", kind=Kind.PII_JSON) is False
    assert engine.can_write(principal=OTHER_WEB, path="anderswo/andere.pii.json", kind=Kind.PII_JSON) is False
    # Nicht-PII-Dateien bleiben von der kind-Regel unberührt.
    assert engine.can_write(principal=ALICE_WEB, path="irgendwo/notiz.md", kind=Kind.MD) is True


def test_kind_gate_denies_even_when_scope_allows() -> None:
    """Exaktes Beispiel aus dem Docstring von acl/engine.py: eine Blanket-Client-Regel
    erlaubt alles, eine kind-Regel verbietet pii.json trotzdem — Kombination ist
    deny-dominant (Schritt 3 der Auswertung: deny wenn Scope ODER Kind-Gate deny sagt)."""
    rules = [
        AclRule(client_id="web-bff", path_prefix="/", read="allow", write="allow"),
        AclRule(kind=Kind.PII_JSON, write="deny"),
    ]
    engine = AclEngine(rules)

    assert engine.can_write(principal=ALICE_WEB, path="person.pii.json", kind=Kind.PII_JSON) is False
    # Andere Kinds bleiben von der Scope-Regel erlaubt.
    assert engine.can_write(principal=ALICE_WEB, path="data.json", kind=Kind.JSON) is True


def test_longer_path_prefix_wins_over_shorter_within_same_scope() -> None:
    rules = [
        AclRule(client_id="web-bff", path_prefix="/", read="allow", write="allow"),
        AclRule(client_id="web-bff", path_prefix="/finanzen/", read="deny", write="deny"),
    ]
    engine = AclEngine(rules)

    assert engine.can_read(principal=ALICE_WEB, path="finanzen/uebersicht.md", kind=Kind.MD) is False
    assert engine.can_read(principal=ALICE_WEB, path="sonstiges/notiz.md", kind=Kind.MD) is True


def test_read_and_write_permissions_evaluated_independently() -> None:
    rules = [AclRule(client_id="web-bff", path_prefix="finanzen/", read="allow", write="deny")]
    engine = AclEngine(rules)

    assert engine.can_read(principal=ALICE_WEB, path="finanzen/uebersicht.md", kind=Kind.MD) is True
    assert engine.can_write(principal=ALICE_WEB, path="finanzen/uebersicht.md", kind=Kind.MD) is False


def test_rule_only_applies_to_matching_user() -> None:
    rules = [AclRule(user_id="human:alice", path_prefix="finanzen/", read="deny")]
    engine = AclEngine(rules)

    assert engine.can_read(principal=ALICE_WEB, path="finanzen/uebersicht.md", kind=Kind.MD) is False
    # Anderer user_id, gleicher Pfad -> Regel greift nicht -> Default-Allow.
    assert engine.can_read(principal=OTHER_WEB, path="finanzen/uebersicht.md", kind=Kind.MD) is True


def test_path_only_rule_without_user_or_client_scope_applies_to_everyone() -> None:
    """Geklärt mit Alice (2026-08-06): eine Regel mit NUR `path_prefix` (kein user_id,
    kein client_id, kein kind) gilt als Blanket-Regel für JEDEN Principal — landet im
    eigenen "Global-Topf" in `_scope_decision()` und fließt in die restriktivste-
    Entscheidung genauso ein wie User-/Client-Topf-Regeln. Vorherige Fassung ließ
    solche Regeln wirkungslos durch alle Töpfe fallen (Default-Allow blieb bestehen,
    obwohl die Regel formal existierte) — das war der eigentliche Bug, nicht das
    gewünschte Verhalten."""
    rules = [AclRule(path_prefix="finanzen/", read="deny", write="deny")]
    engine = AclEngine(rules)

    assert engine.can_read(principal=ALICE_WEB, path="finanzen/uebersicht.md", kind=Kind.MD) is False
    assert engine.can_write(principal=ALICE_WEB, path="finanzen/uebersicht.md", kind=Kind.MD) is False

    # Gilt wirklich für JEDEN Principal, nicht nur für den im Test zufällig verwendeten.
    other = Principal(user_id="human:irgendwer", client_id="irgendein-client")
    assert engine.can_write(principal=other, path="finanzen/uebersicht.md", kind=Kind.MD) is False


# ---- Konflikt ohne Paar-Regel: Interpretationsentscheidung, mit Alice abzugleichen ----------


def test_conflicting_user_and_client_rule_without_pair_rule_is_deny_dominant_and_order_independent() -> None:
    """spec.md §8 sagt nur: fehlt eine Paar-Regel, gilt „die restriktivste Entscheidung
    aus User- und Client-Regeln" (Wortlaut der Spec) — `_scope_decision()` setzt das so
    um, dass unter den je scope-spezifischsten Regeln JEDES `deny` gewinnt, unabhängig
    von der Reihenfolge in der Regelliste (`decisions` wird komplett gesammelt, nicht
    per `max()`-Tie-Break auf der ersten Regel entschieden). Das ist eine bewusste,
    deterministische Auslegung von „restriktivste Entscheidung" — hier FESTGEHALTEN
    (inkl. Beweis der Reihenfolge-Unabhängigkeit), damit ein künftiger Refactor sie
    nicht versehentlich in ein listenreihenfolge-abhängiges Verhalten zurückdreht.
    Von Alice bestätigt (2026-08-06): „irgendein Scope sagt deny -> deny" ist das
    gewünschte Verhalten, auch wenn ein anderer Scope explizit allow sagt.
    """
    user_rule_allow = AclRule(user_id="human:alice", path_prefix="geteilt/", write="allow")
    client_rule_deny = AclRule(client_id="web-bff", path_prefix="geteilt/", write="deny")

    engine_user_first = AclEngine([user_rule_allow, client_rule_deny])
    engine_client_first = AclEngine([client_rule_deny, user_rule_allow])

    assert engine_user_first.can_write(principal=ALICE_WEB, path="geteilt/datei.md", kind=Kind.MD) is False
    assert engine_client_first.can_write(principal=ALICE_WEB, path="geteilt/datei.md", kind=Kind.MD) is False


def test_conflicting_user_and_client_rule_allow_only_when_both_allow() -> None:
    rules = [
        AclRule(user_id="human:alice", path_prefix="geteilt/", write="allow"),
        AclRule(client_id="web-bff", path_prefix="geteilt/", write="allow"),
    ]
    engine = AclEngine(rules)

    assert engine.can_write(principal=ALICE_WEB, path="geteilt/datei.md", kind=Kind.MD) is True


def test_tree_get_hides_entries_without_read_permission() -> None:
    """spec.md §4: GET /tree blendet Einträge ohne Leserecht komplett aus (Filterlogik
    selbst lebt in api/tree.py, hier nur der zugrunde liegende ACL-Baustein isoliert
    verifiziert — der volle HTTP-Pfad wird in test_api_files.py abgedeckt)."""
    rules = [AclRule(client_id="web-bff", path_prefix="geheim/", read="deny")]
    engine = AclEngine(rules)

    assert engine.can_read(principal=ALICE_WEB, path="geheim/akte.md", kind=Kind.MD) is False
    assert engine.can_read(principal=ALICE_WEB, path="oeffentlich/akte.md", kind=Kind.MD) is True


def test_permission_none_field_does_not_count_as_candidate() -> None:
    """Eine Regel, die nur `read` setzt (kein `write`), darf die write-Entscheidung
    nicht beeinflussen — sonst würde eine reine Lese-Deny-Regel versehentlich auch
    Schreibrechte einschränken."""
    rules = [AclRule(client_id="web-bff", path_prefix="finanzen/", read="deny")]
    engine = AclEngine(rules)

    assert engine.can_write(principal=ALICE_WEB, path="finanzen/uebersicht.md", kind=Kind.MD) is True


def test_permission_enum_values() -> None:
    assert Permission.ALLOW.value == "allow"
    assert Permission.DENY.value == "deny"


# ---- Glob-Muster in path_prefix (Alice, 2026-08-07) ------------------------------------


def test_glob_denies_all_kinds_of_a_document_but_specific_allow_overrides_for_one() -> None:
    """Alices Beispiel wörtlich: `mietvertrag*.*` sperrt das ganze Dokument inkl. PII,
    eine spezifischere Regel für genau `mietvertrag.json` erlaubt dieses eine Kind wieder
    -- `mietvertrag.pii.json` bleibt gesperrt, weil dafür keine spezifischere Regel existiert."""
    rules = [
        AclRule(path_prefix="mietvertrag*.*", write="deny"),
        AclRule(path_prefix="mietvertrag.json", write="allow"),
    ]
    engine = AclEngine(rules)

    assert engine.can_write(principal=ALICE_WEB, path="mietvertrag.json", kind=Kind.JSON) is True
    assert engine.can_write(principal=ALICE_WEB, path="mietvertrag.pii.json", kind=Kind.PII_JSON) is False
    assert engine.can_write(principal=ALICE_WEB, path="mietvertrag.md", kind=Kind.MD) is False


def test_single_star_does_not_cross_directory_boundary() -> None:
    rules = [AclRule(path_prefix="kontakte/*.pii.json", write="deny")]
    engine = AclEngine(rules)

    assert engine.can_write(principal=ALICE_WEB, path="kontakte/anna.pii.json", kind=Kind.PII_JSON) is False
    # Eine Ebene tiefer -- ein einzelnes `*` matcht kein `/`, Regel greift nicht -> Default-Allow.
    assert engine.can_write(principal=ALICE_WEB, path="kontakte/archiv/anna.pii.json", kind=Kind.PII_JSON) is True


def test_double_star_crosses_directory_boundaries() -> None:
    rules = [AclRule(path_prefix="kontakte/**.pii.json", write="deny")]
    engine = AclEngine(rules)

    assert engine.can_write(principal=ALICE_WEB, path="kontakte/anna.pii.json", kind=Kind.PII_JSON) is False
    assert engine.can_write(principal=ALICE_WEB, path="kontakte/archiv/2019/anna.pii.json", kind=Kind.PII_JSON) is False


def test_glob_is_a_full_match_not_a_prefix_match() -> None:
    """Anders als ein reiner path_prefix ohne `*` (der als Präfix wirkt) muss ein Glob-Muster
    den vollen Pfad matchen -- `mietvertrag*.md` matcht nicht `mietvertrag.md.bak`."""
    rules = [AclRule(path_prefix="mietvertrag*.md", write="deny")]
    engine = AclEngine(rules)

    assert engine.can_write(principal=ALICE_WEB, path="mietvertrag.md", kind=Kind.MD) is False
    assert engine.can_write(principal=ALICE_WEB, path="mietvertrag.md.bak", kind=Kind.BINARY) is True


def test_more_literal_characters_win_between_two_matching_globs() -> None:
    rules = [
        AclRule(path_prefix="*.json", write="deny"),
        AclRule(path_prefix="mietvertrag.json", write="allow"),
    ]
    engine = AclEngine(rules)

    assert engine.can_write(principal=ALICE_WEB, path="mietvertrag.json", kind=Kind.JSON) is True
    assert engine.can_write(principal=ALICE_WEB, path="sonstiges.json", kind=Kind.JSON) is False
