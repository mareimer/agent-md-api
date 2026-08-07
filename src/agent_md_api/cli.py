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
