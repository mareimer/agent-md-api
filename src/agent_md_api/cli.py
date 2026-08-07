"""Bootstrap-CLI für die Client-Registry (spec.md §10a).

Deckt den in README.md "Known gaps" genannten fehlenden Bootstrap-Schritt ab:
den ersten Client anlegen, ohne dafür Python-Code schreiben zu müssen. Nutzt
dieselbe `ClientRegistry` wie die laufende App — kein separater Codepfad.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from agent_md_api.domain.errors import AgentApiError
from agent_md_api.domain.models import ClientType

from .auth.client_registry import ClientRegistry
from .auth.tokens import generate_keypair_pem


def _registry_path(args: argparse.Namespace) -> Path:
    raw = args.registry_path or os.environ.get("AGENT_API_CLIENT_REGISTRY_PATH")
    if not raw:
        print(
            "Fehler: Registry-Pfad fehlt. Mit --registry-path angeben oder "
            "AGENT_API_CLIENT_REGISTRY_PATH setzen.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return Path(raw)


def _cmd_create_client(args: argparse.Namespace) -> None:
    registry = ClientRegistry(_registry_path(args))
    entry, api_key = registry.create_client(
        client_id=args.client_id,
        type=ClientType(args.type),
        issues_user_tokens=args.issues_user_tokens,
        fixed_user_id=args.fixed_user_id,
    )
    print(f"Client '{entry.client_id}' angelegt (type={entry.type.value}).")
    print(f"API-Key (nur jetzt sichtbar, sicher aufbewahren):\n{api_key}")


def _cmd_list_clients(args: argparse.Namespace) -> None:
    registry = ClientRegistry(_registry_path(args))
    entries = registry.list_all()
    if not entries:
        print("Keine Clients registriert.")
        return
    for entry in entries:
        status = "revoked" if entry.revoked else "active"
        print(f"{entry.client_id}\ttype={entry.type.value}\tstatus={status}\tcreated_at={entry.created_at}")


def _cmd_revoke_client(args: argparse.Namespace) -> None:
    registry = ClientRegistry(_registry_path(args))
    registry.revoke_client(args.client_id)
    print(f"Client '{args.client_id}' widerrufen.")


def _cmd_rotate_key(args: argparse.Namespace) -> None:
    registry = ClientRegistry(_registry_path(args))
    api_key = registry.rotate_api_key(args.client_id)
    print(f"Neuer API-Key für '{args.client_id}' (nur jetzt sichtbar):\n{api_key}")


def _cmd_generate_signing_key(args: argparse.Namespace) -> None:
    """Erzeugt ein Ed25519-Keypair, schreibt den privaten Schlüssel in eine Datei und
    registriert den öffentlichen Schlüssel direkt bei der Agent-API (spec.md §10b)."""
    private_key_path = Path(args.private_key_out)
    if private_key_path.exists() and not args.force:
        print(
            f"Fehler: '{private_key_path}' existiert bereits. Mit --force überschreiben.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    registry = ClientRegistry(_registry_path(args))
    private_pem, public_pem = generate_keypair_pem()
    key = registry.add_signing_key(args.client_id, kid=args.kid, public_key=public_pem)

    private_key_path.parent.mkdir(parents=True, exist_ok=True)
    private_key_path.write_text(private_pem, encoding="utf-8")
    try:
        private_key_path.chmod(0o600)
    except NotImplementedError:
        pass  # z.B. auf manchen Windows-Dateisystemen nicht unterstützt

    print(f"Signing-Key '{key.kid}' für Client '{args.client_id}' registriert.")
    print(f"Privater Schlüssel geschrieben nach: {private_key_path}")


def _cmd_add_signing_key(args: argparse.Namespace) -> None:
    """Registriert einen bereits vorhandenen öffentlichen Schlüssel (z.B. von einem Client,
    der sein Keypair selbst erzeugt hat, statt über generate-signing-key)."""
    public_key_pem = Path(args.public_key_file).read_text(encoding="utf-8")
    registry = ClientRegistry(_registry_path(args))
    key = registry.add_signing_key(args.client_id, kid=args.kid, public_key=public_key_pem)
    print(f"Signing-Key '{key.kid}' für Client '{args.client_id}' registriert.")


def _cmd_revoke_signing_key(args: argparse.Namespace) -> None:
    registry = ClientRegistry(_registry_path(args))
    registry.revoke_signing_key(args.client_id, args.kid)
    print(f"Signing-Key '{args.kid}' für Client '{args.client_id}' widerrufen.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-md-api",
        description="Bootstrap-/Verwaltungs-CLI für die Agent-API Client-Registry.",
    )
    parser.add_argument(
        "--registry-path",
        help="Pfad zur clients.json (Default: AGENT_API_CLIENT_REGISTRY_PATH aus der Umgebung).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create-client", help="Neuen Client registrieren und API-Key ausgeben.")
    create.add_argument("client_id")
    create.add_argument(
        "--type",
        choices=[t.value for t in ClientType],
        default=ClientType.BFF.value,
        help="Client-Typ (Default: bff).",
    )
    create.add_argument(
        "--fixed-user-id",
        help="Pflicht bei --type=autonomous-agent, sonst unzulässig (spec.md §10).",
    )
    create.add_argument(
        "--issues-user-tokens",
        action="store_true",
        help="Client darf X-User-Token für Menschen ausstellen (z.B. Web-Frontend, WebDAV-Bridge).",
    )
    create.set_defaults(func=_cmd_create_client)

    listing = subparsers.add_parser("list-clients", help="Alle registrierten Clients auflisten.")
    listing.set_defaults(func=_cmd_list_clients)

    revoke = subparsers.add_parser("revoke-client", help="Client widerrufen (API-Key wird ungültig).")
    revoke.add_argument("client_id")
    revoke.set_defaults(func=_cmd_revoke_client)

    rotate = subparsers.add_parser("rotate-key", help="Neuen API-Key für einen bestehenden Client ausstellen.")
    rotate.add_argument("client_id")
    rotate.set_defaults(func=_cmd_rotate_key)

    generate_key = subparsers.add_parser(
        "generate-signing-key",
        help="Ed25519-Keypair erzeugen, privaten Schlüssel in Datei schreiben, Public Key registrieren "
        "(spec.md §10b — für Clients, die X-User-Token ausstellen, z.B. die WebDAV-Bridge).",
    )
    generate_key.add_argument("client_id")
    generate_key.add_argument("--kid", required=True, help="Key-ID, z.B. 'bridge-key-1'.")
    generate_key.add_argument(
        "--private-key-out",
        required=True,
        help="Zielpfad für die PEM-Datei mit dem privaten Schlüssel (verlässt die Agent-API danach nie mehr).",
    )
    generate_key.add_argument("--force", action="store_true", help="Vorhandene Datei am Zielpfad überschreiben.")
    generate_key.set_defaults(func=_cmd_generate_signing_key)

    add_key = subparsers.add_parser(
        "add-signing-key",
        help="Bereits vorhandenen öffentlichen Schlüssel (PEM-Datei) bei einem Client registrieren.",
    )
    add_key.add_argument("client_id")
    add_key.add_argument("--kid", required=True)
    add_key.add_argument("--public-key-file", required=True, help="Pfad zur PEM-Datei mit dem Public Key.")
    add_key.set_defaults(func=_cmd_add_signing_key)

    revoke_key = subparsers.add_parser("revoke-signing-key", help="Signing-Key eines Clients widerrufen.")
    revoke_key.add_argument("client_id")
    revoke_key.add_argument("--kid", required=True)
    revoke_key.set_defaults(func=_cmd_revoke_signing_key)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except AgentApiError as exc:
        print(f"Fehler: {exc.message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
