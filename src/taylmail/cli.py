"""taylmail serve | a2u"""

import sys

from . import config

USAGE = """\
usage: taylmail serve                                         (the daemon; runs agent turns)
       taylmail a2u [-i] [recipient...] < message             (git send-email --sendmail-cmd)
"""


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE, end="")
        return 0 if argv else 2
    cmd, rest = argv[0], argv[1:]
    cfg = config.load()
    if cmd == "serve" and not rest:
        from . import daemon
        return daemon.run(cfg)
    if cmd == "a2u":
        from . import a2u
        return a2u.main(cfg, rest)
    print(USAGE, end="", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
