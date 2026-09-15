"""Argument helpers shared by the entry points."""

from __future__ import annotations

import argparse


def add_ticket_id_argument(parser: argparse.ArgumentParser) -> None:
    """Ticket id as a positional or as --ticket-id / --ticket_id.

    The skill script runner passes only named flags and callers write the name both ways, so
    both are accepted rather than failing on a hyphen.
    """
    parser.add_argument("ticket_id_pos", nargs="?", type=int, metavar="ticket_id")
    parser.add_argument(
        "--ticket-id", "--ticket_id", type=int, default=None, dest="ticket_id_opt", metavar="ID"
    )


def resolved_ticket_id(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    ticket_id = args.ticket_id_opt if args.ticket_id_opt is not None else args.ticket_id_pos
    if ticket_id is None:
        parser.error("ticket id required: pass it positionally or as --ticket-id N")
    return int(ticket_id)


__all__ = ["add_ticket_id_argument", "resolved_ticket_id"]
