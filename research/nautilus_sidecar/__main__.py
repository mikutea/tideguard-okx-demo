from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from .protocol import ProtocolError, canonical_json, decode_canonical_request
from .service import build_error_response, build_self_check_request, handle_request


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the credential-free Nautilus sidecar protocol PoC with no "
            "authorized network use or imported network adapters."
        )
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--self-test",
        action="store_true",
        help="Validate the canonical protocol without requiring NautilusTrader.",
    )
    source.add_argument(
        "--input",
        type=Path,
        help="Read one canonical JSON request from a project-local file; use '-' for stdin.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.self_test:
            request = build_self_check_request()
        elif str(args.input) == "-":
            request = decode_canonical_request(sys.stdin.buffer.read())
        else:
            request = decode_canonical_request(args.input.read_bytes())
        response = handle_request(request)
    except ProtocolError as error:
        sys.stdout.write(canonical_json(build_error_response(error)) + "\n")
        return 2
    except OSError:
        error = ProtocolError("LOCAL_IO_ERROR", "project-local request could not be read")
        sys.stdout.write(canonical_json(build_error_response(error)) + "\n")
        return 2
    sys.stdout.write(canonical_json(response) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
