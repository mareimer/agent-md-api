"""Benutzerverwaltungs-CLI für die eigene, kleine Zugangsebene der WebDAV-Bridge
(spec.md §14.2) — Benutzername + Personal-Access-Token statt der internen
Agent-API-Tokens, weil der Windows/macOS-WebDAV-Client nur Basic-Auth kann.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .user_access import UnknownUserError, UserAccessRegistry


def _users_path(args: argparse.Namespace) -> Path:
    raw = args.users_path or os.environ.get("WEBDAV_BRIDGE_USER_ACCESS_PATH")
    if not raw:
        print(
            "Fehler: Pfad zur Benutzerverwaltung fehlt. Mit --users-path angeben oder "
            "WEBDAV_BRIDGE_USER_ACCESS_PATH setzen.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return Path(raw)


def _cmd_create_user(args: argparse.Namespace) -> None:
    registry = UserAccessRegistry(_users_path(args))
    pat = registry.create_user(username=args.username, agent_api_user_id=args.agent_api_user_id)
    print(f"Benutzer '{args.username}' angelegt (agent_api_user_id={args.agent_api_user_id}).")
    print(f"Personal-Access-Token (nur jetzt sichtbar, das ist das WebDAV-Passwort):\n{pat}")


def _cmd_rotate_token(args: argparse.Namespace) -> None:
    registry = UserAccessRegistry(_users_path(args))
    pat = registry.rotate_token(args.username)
    print(f"Neues Personal-Access-Token für '{args.username}' (nur jetzt sichtbar):\n{pat}")


def _cmd_revoke_user(args: argparse.Namespace) -> None:
    registry = UserAccessRegistry(_users_path(args))
    registry.revoke_user(args.username)
    print(f"Benutzer '{args.username}' widerrufen.")


def _cmd_list_users(args: argparse.Namespace) -> None:
    registry = UserAccessRegistry(_users_path(args))
    usernames = registry.list_usernames()
    if not usernames:
        print("Keine Benutzer angelegt.")
        return
    for username in usernames:
        print(username)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="webdav-bridge-cli",
        description="Benutzerverwaltung für die WebDAV-Bridge (Benutzername + Personal-Access-Token).",
    )
    parser.add_argument(
        "--users-path",
        help="Pfad zur webdav-users.json (Default: WEBDAV_BRIDGE_USER_ACCESS_PATH aus der Umgebung).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create-user", help="Neuen WebDAV-Zugang anlegen und PAT ausgeben.")
    create.add_argument("username")
    create.add_argument(
        "--agent-api-user-id",
        required=True,
        help="user_id, unter der die Agent-API die Zugriffe dieses Menschen sieht, z.B. 'human:marko'.",
    )
    create.set_defaults(func=_cmd_create_user)

    rotate = subparsers.add_parser("rotate-token", help="Neues PAT für einen bestehenden Benutzer ausstellen.")
    rotate.add_argument("username")
    rotate.set_defaults(func=_cmd_rotate_token)

    revoke = subparsers.add_parser("revoke-user", help="WebDAV-Zugang widerrufen.")
    revoke.add_argument("username")
    revoke.set_defaults(func=_cmd_revoke_user)

    listing = subparsers.add_parser("list-users", help="Alle angelegten Benutzernamen auflisten.")
    listing.set_defaults(func=_cmd_list_users)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except UnknownUserError as exc:
        print(f"Fehler: Benutzer '{exc}' nicht gefunden.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
