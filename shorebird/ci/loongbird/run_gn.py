#!/usr/bin/env python3
"""Cross-platform wrapper around engine/src/flutter/tools/gn.

Avoids PowerShell / Bash quoting pitfalls when passing string-valued --gn-args
such as engine_version="..." alongside verify_sdk_hash=false.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def main() -> int:
  parser = argparse.ArgumentParser(description='Invoke Flutter tools/gn cleanly')
  parser.add_argument(
      '--engine-src',
      type=Path,
      default=Path('engine/src'),
      help='Path to engine/src directory',
  )
  parser.add_argument(
      '--engine-version-file',
      type=Path,
      default=Path('bin/internal/engine.version'),
      help='Path to Flutter bin/internal/engine.version',
  )
  parser.add_argument(
      '--engine-version',
      default=None,
      help='Explicit engine_version override (defaults to reading --engine-version-file)',
  )
  parser.add_argument(
      'gn_flags',
      nargs=argparse.REMAINDER,
      help='Flags forwarded directly to flutter/tools/gn (prefix with --)',
  )
  args = parser.parse_args()

  engine_rev = (
      args.engine_version.strip()
      if args.engine_version
      else args.engine_version_file.read_text(encoding='utf-8').strip()
  )
  if not engine_rev:
    raise RuntimeError('Resolved empty engine_version')

  extra = list(args.gn_flags)
  if extra and extra[0] == '--':
    extra = extra[1:]

  engine_src = args.engine_src.resolve()
  gn_script = engine_src / 'flutter' / 'tools' / 'gn'
  if not gn_script.is_file():
    raise RuntimeError(f'GN script not found: {gn_script}')

  cmd = [
      sys.executable,
      str(gn_script),
      '--no-rbe',
      '--no-enable-unittests',
      '--no-prebuilt-dart-sdk',
      *extra,
      '--gn-args',
      'verify_sdk_hash=false',
      '--gn-args',
      f'engine_version="{engine_rev}"',
  ]
  print(f'[run_gn] cwd={engine_src} cmd={cmd}', flush=True)
  res = subprocess.run(cmd, cwd=str(engine_src), check=False)
  return res.returncode


if __name__ == '__main__':
  sys.exit(main())
