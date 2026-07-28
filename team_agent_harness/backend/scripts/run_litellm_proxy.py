from __future__ import annotations

import sys

import litellm


if __name__ == "__main__":
    litellm.run_server.main(args=sys.argv[1:], standalone_mode=True)
