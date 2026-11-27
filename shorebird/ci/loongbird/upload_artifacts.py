#!/usr/bin/env python3
"""Cross-platform artifact packager and uploader for Loongbird Engine CI.

Supports:
1. Packaging directories into zip archives (preserving POSIX file permissions
   and symlinks for dart-sdk archives on Linux/macOS and clean paths on Windows).
2. Staging artifacts into a gateway-relative directory tree so GitHub Actions
   upload-artifact preserves the exact upload layout.
3. Streaming uploads to Loongbird's self-hosted Engine Artifact Gateway:
   PUT <gateway-url>/api/v1/engine-artifacts/<rel-path>
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path


def zip_directory(src_dir: Path, dst_zip: Path, root_name: str | None = None) -> None:
  """Create a zip archive of src_dir with top-level directory root_name."""
  src_dir = src_dir.resolve()
  if not src_dir.is_dir():
    raise FileNotFoundError(f'Source directory does not exist: {src_dir}')
  dst_zip.parent.mkdir(parents=True, exist_ok=True)
  top = root_name or src_dir.name

  with zipfile.ZipFile(dst_zip, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
    for root, dirs, files in os.walk(src_dir, followlinks=False):
      dirs.sort()
      files.sort()
      rel_root = Path(root).relative_to(src_dir)

      # Preserve directory symlinks if present
      for d in list(dirs):
        full_d = Path(root) / d
        if full_d.is_symlink():
          dirs.remove(d)
          arcname = (Path(top) / rel_root / d).as_posix()
          zi = zipfile.ZipInfo(arcname)
          zi.create_system = 3  # Unix
          zi.external_attr = (stat.S_IFLNK | 0o777) << 16
          zf.writestr(zi, os.readlink(full_d))

      for fname in files:
        full_path = Path(root) / fname
        arcname = (Path(top) / rel_root / fname).as_posix()
        if full_path.is_symlink():
          zi = zipfile.ZipInfo(arcname)
          zi.create_system = 3
          zi.external_attr = (stat.S_IFLNK | 0o777) << 16
          zf.writestr(zi, os.readlink(full_path))
        else:
          zi = zipfile.ZipInfo.from_file(full_path, arcname)
          zi.compress_type = zipfile.ZIP_DEFLATED
          if os.name != 'nt':
            st = full_path.stat()
            zi.create_system = 3
            zi.external_attr = (st.st_mode & 0xFFFF) << 16
          with full_path.open('rb') as f:
            zf.writestr(zi, f.read(), compress_type=zipfile.ZIP_DEFLATED)

  size_mb = dst_zip.stat().st_size / (1024 * 1024)
  print(f'[zip] {src_dir} -> {dst_zip} ({size_mb:.2f} MiB)')


def zip_single_file(src_file: Path, dst_zip: Path, arcname: str | None = None) -> None:
  """Create a zip archive containing a single binary (e.g. patch / patch.exe)."""
  src_file = src_file.resolve()
  if not src_file.is_file():
    raise FileNotFoundError(f'Source file does not exist: {src_file}')
  dst_zip.parent.mkdir(parents=True, exist_ok=True)
  name = arcname or src_file.name

  with zipfile.ZipFile(dst_zip, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
    zi = zipfile.ZipInfo.from_file(src_file, name)
    zi.compress_type = zipfile.ZIP_DEFLATED
    if os.name != 'nt':
      st = src_file.stat()
      zi.create_system = 3
      zi.external_attr = (st.st_mode & 0xFFFF) << 16
    with src_file.open('rb') as f:
      zf.writestr(zi, f.read(), compress_type=zipfile.ZIP_DEFLATED)

  size_mb = dst_zip.stat().st_size / (1024 * 1024)
  print(f'[zip-file] {src_file} ({name}) -> {dst_zip} ({size_mb:.2f} MiB)')


def stage_mapping(stage_dir: Path, mapping: str) -> Path:
  if '=' not in mapping:
    raise ValueError(f'Invalid --map entry (expected SRC=REL_DST): {mapping}')
  src_str, rel_dst = mapping.split('=', 1)
  src = Path(src_str)
  rel_dst = rel_dst.lstrip('/')
  dst = stage_dir / Path(*rel_dst.split('/'))
  dst.parent.mkdir(parents=True, exist_ok=True)
  if not src.is_file():
    raise FileNotFoundError(f'Mapped artifact does not exist: {src}')
  shutil.copy2(src, dst)
  size_mb = dst.stat().st_size / (1024 * 1024)
  print(f'[stage] {src} -> {rel_dst} ({size_mb:.2f} MiB)')
  return dst


def curl_head_exists(url: str, token: str) -> bool:
  cmd = [
      'curl',
      '-sS',
      '-o',
      os.devnull,
      '-w',
      '%{http_code}',
      '-I',
      '--connect-timeout',
      '20',
      '-H',
      f'Authorization: Bearer {token}',
      url,
  ]
  res = subprocess.run(cmd, capture_output=True, text=True, check=False)
  return res.returncode == 0 and res.stdout.strip() == '200'


def upload_staged_dir(
    stage_dir: Path,
    gateway_url: str,
    token: str,
    skip_if_exists: bool,
    allow_413_fallback: bool,
) -> int:
  gateway_url = gateway_url.rstrip('/')
  files = sorted(p for p in stage_dir.rglob('*') if p.is_file())
  if not files:
    print(f'[upload] No files found under {stage_dir}')
    return 1

  skipped_413: list[str] = []
  for fpath in files:
    rel_path = fpath.relative_to(stage_dir).as_posix()
    url = f'{gateway_url}/api/v1/engine-artifacts/{rel_path}'
    size_mb = fpath.stat().st_size / (1024 * 1024)

    if skip_if_exists and curl_head_exists(url, token):
      print(f'[upload] SKIP (already exists): {rel_path}')
      continue

    print(f'[upload] PUT {rel_path} ({size_mb:.2f} MiB) -> {url}', flush=True)
    body_file = stage_dir / '.curl_resp.tmp'
    cmd = [
        'curl',
        '-sS',
        '--retry',
        '3',
        '--retry-delay',
        '5',
        '--connect-timeout',
        '30',
        '--max-time',
        '1800',
        '-X',
        'PUT',
        '-H',
        f'Authorization: Bearer {token}',
        '-H',
        'Content-Type: application/octet-stream',
        '--upload-file',
        str(fpath),
        '-o',
        str(body_file),
        '-w',
        '%{http_code}',
        url,
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    http_code = res.stdout.strip()
    body = body_file.read_text(errors='replace') if body_file.exists() else ''
    if body_file.exists():
      body_file.unlink()

    if res.returncode == 0 and http_code in ('200', '201', '204'):
      print(f'[upload] OK ({http_code}): {body.strip()}')
      continue

    if http_code == '413' and allow_413_fallback:
      print(
          f'[upload] WARN HTTP 413 (CDN body limit) for {rel_path} ({size_mb:.2f} MiB). '
          'Artifact is still saved in GitHub Actions Artifacts for local upload.'
      )
      skipped_413.append(rel_path)
      continue

    print(
        f'[upload] FAILED {rel_path}: rc={res.returncode} http={http_code}\n'
        f'stderr: {res.stderr}\nbody: {body}',
        file=sys.stderr,
    )
    return 1

  if skipped_413:
    print(
        '[upload] Completed with HTTP 413 fallback on large files: '
        + ', '.join(skipped_413)
    )
  return 0


def main() -> int:
  parser = argparse.ArgumentParser(description='Package and upload Loongbird Engine artifacts.')
  parser.add_argument('--zip-dir', action='append', default=[],
                      help='Zip a directory: SRC_DIR=DST_ZIP[:ROOT_NAME]')
  parser.add_argument('--zip-file', action='append', default=[],
                      help='Zip a single file: SRC_FILE=DST_ZIP[:ARCNAME]')
  parser.add_argument('--stage-dir', type=Path, help='Directory to stage gateway-relative artifacts')
  parser.add_argument('--map', action='append', default=[],
                      help='Stage artifact mapping: LOCAL_PATH=GATEWAY_REL_PATH')
  parser.add_argument('--upload-dir', type=Path, help='Upload all files from this staged directory')
  parser.add_argument('--gateway-url', default=os.environ.get('LOONGBIRD_GATEWAY_URL', ''),
                      help='Base URL of Loongbird gateway (e.g. https://api.loongbird.cyjay.fun)')
  parser.add_argument('--token', default=os.environ.get('LOONGBIRD_API_TOKEN', ''),
                      help='Bearer API token (sb_api_...)')
  parser.add_argument('--skip-if-exists', action='store_true',
                      help='Skip uploading artifacts that already return HTTP 200 on HEAD')
  parser.add_argument('--allow-413-fallback', action='store_true',
                      help='Warn instead of failing on HTTP 413 (useful behind 100 MiB CDNs)')
  args = parser.parse_args()

  for spec in args.zip_dir:
    src_s, rest = spec.split('=', 1)
    if ':' in rest and not (len(rest) >= 2 and rest[1] == ':' and rest.count(':') == 1):
      dst_s, root_name = rest.rsplit(':', 1)
    else:
      dst_s, root_name = rest, None
    zip_directory(Path(src_s), Path(dst_s), root_name)

  for spec in args.zip_file:
    src_s, rest = spec.split('=', 1)
    if ':' in rest and not (len(rest) >= 2 and rest[1] == ':' and rest.count(':') == 1):
      dst_s, arcname = rest.rsplit(':', 1)
    else:
      dst_s, arcname = rest, None
    zip_single_file(Path(src_s), Path(dst_s), arcname)

  if args.map:
    if not args.stage_dir:
      parser.error('--stage-dir is required when --map is specified')
    args.stage_dir.mkdir(parents=True, exist_ok=True)
    for m in args.map:
      stage_mapping(args.stage_dir, m)

  if args.upload_dir:
    if not args.gateway_url or not args.token:
      print('[upload] Skipping gateway upload (LOONGBIRD_GATEWAY_URL or LOONGBIRD_API_TOKEN not set).')
      return 0
    return upload_staged_dir(
        args.upload_dir,
        args.gateway_url,
        args.token,
        skip_if_exists=args.skip_if_exists,
        allow_413_fallback=args.allow_413_fallback,
    )

  return 0


if __name__ == '__main__':
  sys.exit(main())
