"""taylmail u2a | a2u | run"""

import sys

from . import config

USAGE = """\
usage: taylmail u2a [--foreground] [recipient...] < message   (aerc `outgoing`)
       taylmail a2u [-i] [recipient...] < message             (git send-email --sendmail-cmd)
       taylmail run SPOOLED.eml                              (one agent turn; u2a starts this)
"""


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE, end="")
        return 0 if argv else 2
    cmd, rest = argv[0], argv[1:]
    cfg = config.load()
    if cmd == "u2a":
        from . import u2a
        return u2a.main(cfg, rest)
    if cmd == "a2u":
        from . import a2u
        return a2u.main(cfg, rest)
    if cmd == "run" and len(rest) == 1:
        from . import agent
        return agent.run(cfg, rest[0])
    print(USAGE, end="", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
