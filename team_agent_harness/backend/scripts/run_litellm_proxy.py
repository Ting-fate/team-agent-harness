from __future__ import annotations

import os
import sys


PROJECT_ROOT_ARGUMENT = "--team-agent-project-root"


def _validated_litellm_args(argv: list[str]) -> list[str]:
    if len(argv) < 2 or argv[0] != PROJECT_ROOT_ARGUMENT:
        raise SystemExit("LiteLLM launcher project identity is missing.")

    expected_root = os.path.normcase(os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))
    actual_root = os.path.normcase(os.path.abspath(argv[1]))
    if actual_root != expected_root:
        raise SystemExit("LiteLLM launcher project identity does not match this runner.")
    return argv[2:]


if __name__ == "__main__":
    import litellm

    litellm.run_server.main(args=_validated_litellm_args(sys.argv[1:]), standalone_mode=True)
