"""Exit codes: BSD sysexits.h plus POSIX signal convention.

This table IS the CLI's error contract — the caller classifies failures from
these codes (and from stderr briefs), never from parsing prose.

  0   ok
  64  EX_USAGE    bad flags / unknown subcommand / contradictory arguments
  65  EX_DATAERR  data-level failure (edit target not found or ambiguous,
                  requested line range exceeds the read budget, bad stdin bytes)
  66  EX_NOINPUT  input file (or search root) missing
  70  EX_SOFTWARE internal invariant broken (bug — report it)
  74  EX_IOERR    read/write I/O failure
  77  EX_NOPERM   daemon fence refusal (path matches a --protect glob) — a
                  flippable default; no direct-exec path raises it today
  130 128+SIGINT  aborted by the caller
"""

EX_OK = 0
EX_USAGE = 64
EX_DATAERR = 65
EX_NOINPUT = 66
EX_SOFTWARE = 70
EX_IOERR = 74
EX_NOPERM = 77
EX_SIGINT = 130
