#!/usr/bin/env python3
"""Automated Linux / Windows Desktop Zero-Local-Engine E2E verifier for Loongbird.

Verifies the complete self-hosted hot-update loop without --local-engine:
1. Self-hosted CLI bootstrap (`shorebird --version` pulls Dart SDK from gateway).
2. Isolated Flutter desktop app creation + RSA-2048 signing keypair generation.
3. `shorebird release --platforms=<linux|windows> --public-key-path=...`.
4. Code mutation + `shorebird patch --platforms=<linux|windows> --private-key-path=...`.
5. 3-run desktop binary verification:
   - Run 1: Boots base release (marker=BASE, patch=0, 6/6 stress pass), downloads & installs Patch 1.
   - Run 2: Boots Patch 1 (marker=PATCH1, patch=1, 6/6 stress pass).
   - Run 3: Re-boots Patch 1 (marker=PATCH1, patch=1, 6/6 stress pass, zero rollback).
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path


MAIN_DART = r'''import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:shorebird_code_push/shorebird_code_push.dart';

import 'patch_workload.dart';

void _emit(String msg) {
  // ignore: avoid_print
  print(msg);
  try {
    stdout.writeln(msg);
  } catch (_) {}
  final logPath = Platform.environment['LOONGBIRD_E2E_LOG'];
  if (logPath != null && logPath.isNotEmpty) {
    File(logPath).writeAsStringSync('$msg\n', mode: FileMode.append, flush: true);
  }
}

void main() {
  _emit('[E2E-PHASED2] BOOT marker=$workloadMarker');
  runApp(const E2EDesktopApp());
}

class E2EDesktopApp extends StatefulWidget {
  const E2EDesktopApp({super.key});

  @override
  State<E2EDesktopApp> createState() => _E2EDesktopAppState();
}

class _E2EDesktopAppState extends State<E2EDesktopApp> {
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) => _run());
  }

  Future<void> _run() async {
    try {
      final updater = ShorebirdUpdater();
      final current = await updater.readCurrentPatch();
      final results = await runAllStressTests();
      final passed = results.values.where((v) => v.startsWith('PASS')).length;
      final total = results.length;
      final detail = results.entries.map((e) => '${e.key}=${e.value}').join(' | ');
      _emit(
        '[E2E-PHASED2] STRESS_RESULT marker=$workloadMarker '
        'patch=${current?.number ?? 0} passed=$passed/$total $detail',
      );

      if (Platform.environment['LOONGBIRD_E2E_DO_UPDATE'] == '1') {
        _emit('[E2E-PHASED2] UPDATE_START');
        try {
          await updater.update();
        } catch (e) {
          _emit('[E2E-PHASED2] UPDATE_ERR=$e');
        }
        Patch? next;
        for (var i = 0; i < 30; i++) {
          next = await updater.readNextPatch();
          if (next != null) break;
          await Future<void>.delayed(const Duration(seconds: 1));
        }
        _emit('[E2E-PHASED2] UPDATE_DONE nextPatch=${next?.number ?? 0}');
      } else {
        await Future<void>.delayed(const Duration(seconds: 2));
      }
    } catch (e, st) {
      _emit('[E2E-PHASED2] FATAL err=$e\n$st');
    } finally {
      _emit('[E2E-PHASED2] EXITING');
      exit(0);
    }
  }

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      home: Scaffold(body: Center(child: Text(workloadMarker))),
    );
  }
}
'''

PATCH_WORKLOAD_BASE = r'''const String workloadMarker = 'BASE';

Future<Map<String, String>> runAllStressTests() async {
  final results = <String, String>{};
  var acc = 0;
  for (var i = 1; i <= 100; i++) {
    acc += i * 2;
  }
  results['r1'] = acc == 10100 ? 'PASS($acc)' : 'FAIL($acc)';

  final asyncVal = await Future<int>.delayed(
    const Duration(milliseconds: 20),
    () => 42,
  );
  results['r2'] = asyncVal == 42 ? 'PASS($asyncVal)' : 'FAIL($asyncVal)';

  var caught = false;
  try {
    throw StateError('expected');
  } on StateError {
    caught = true;
  }
  results['r3'] = caught ? 'PASS' : 'FAIL';

  final list = List<int>.generate(10, (i) => i * i);
  results['r4'] = list.last == 81 ? 'PASS(${list.last})' : 'FAIL';

  final re = RegExp(r'^BASE-(\d+)$');
  final m = re.firstMatch('$workloadMarker-123');
  results['r5'] = m?.group(1) == '123' ? 'PASS' : 'FAIL';

  int fib(int n) => n <= 1 ? n : fib(n - 1) + fib(n - 2);
  final f = fib(12);
  results['r6'] = f == 144 ? 'PASS($f)' : 'FAIL($f)';
  return results;
}
'''

PATCH_WORKLOAD_PATCH1 = r'''const String workloadMarker = 'PATCH1';

Future<Map<String, String>> runAllStressTests() async {
  final results = <String, String>{};
  var acc = 0;
  for (var i = 1; i <= 100; i++) {
    acc += i * 3;
  }
  results['r1'] = acc == 15150 ? 'PASS($acc)' : 'FAIL($acc)';

  final asyncVal = await Future<int>.delayed(
    const Duration(milliseconds: 20),
    () => 84,
  );
  results['r2'] = asyncVal == 84 ? 'PASS($asyncVal)' : 'FAIL($asyncVal)';

  var caught = false;
  try {
    throw StateError('patched-expected');
  } on StateError {
    caught = true;
  }
  results['r3'] = caught ? 'PASS' : 'FAIL';

  final list = List<int>.generate(10, (i) => i * i + 1);
  results['r4'] = list.last == 82 ? 'PASS(${list.last})' : 'FAIL';

  final re = RegExp(r'^PATCH1-(\d+)$');
  final m = re.firstMatch('$workloadMarker-456');
  results['r5'] = m?.group(1) == '456' ? 'PASS' : 'FAIL';

  int fib(int n) => n <= 1 ? n : fib(n - 1) + fib(n - 2);
  final f = fib(13);
  results['r6'] = f == 233 ? 'PASS($f)' : 'FAIL($f)';
  return results;
}
'''


def run_cmd(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 1200,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
  print(f'[cmd] ({cwd or Path.cwd()}) {" ".join(cmd)}', flush=True)
  res = subprocess.run(
      cmd,
      cwd=str(cwd) if cwd else None,
      env=env,
      capture_output=True,
      text=True,
      timeout=timeout,
      check=False,
  )
  if res.stdout:
    print(res.stdout, flush=True)
  if res.stderr:
    print(res.stderr, file=sys.stderr, flush=True)
  if check and res.returncode != 0:
    raise RuntimeError(f'Command failed (rc={res.returncode}): {" ".join(cmd)}')
  return res


def assert_no_upstream_shorebird_urls(output: str, label: str) -> None:
  bad = re.findall(r'https?://[^\s"\']*(?:shorebird\.dev|shorebirdtech)[^\s"\']*', output)
  if bad:
    raise RuntimeError(f'[{label}] Detected forbidden upstream shorebird URLs: {bad[:5]}')


def generate_rsa_keypair(key_dir: Path) -> tuple[Path, Path]:
  priv = key_dir / 'private.pem'
  pub = key_dir / 'public.pem'
  openssl = shutil.which('openssl')
  if not openssl and os.name == 'nt':
    for candidate in (
        r'C:\Program Files\Git\usr\bin\openssl.exe',
        r'C:\Program Files\OpenSSL-Win64\bin\openssl.exe',
    ):
      if Path(candidate).exists():
        openssl = candidate
        break
  if not openssl:
    raise RuntimeError('openssl not found in PATH')

  run_cmd([openssl, 'genrsa', '-out', str(priv), '2048'])
  run_cmd([openssl, 'rsa', '-in', str(priv), '-pubout', '-out', str(pub)])
  return priv, pub


def launch_desktop_app(
    platform: str,
    binary: Path,
    log_file: Path,
    env: dict[str, str],
    timeout: int = 90,
) -> str:
  if log_file.exists():
    log_file.unlink()

  run_env = dict(env)
  run_env['LOONGBIRD_E2E_LOG'] = str(log_file)

  if platform == 'linux':
    run_env['LIBGL_ALWAYS_SOFTWARE'] = '1'
    run_env['GDK_BACKEND'] = 'x11'
    cmd = ['xvfb-run', '-a', '-s', '-screen 0 1280x720x24', str(binary)]
  else:
    cmd = [str(binary)]

  print(f'[run] Launching {binary} (log={log_file})...', flush=True)
  proc = subprocess.Popen(
      cmd,
      cwd=str(binary.parent),
      env=run_env,
      stdout=subprocess.PIPE,
      stderr=subprocess.STDOUT,
      text=True,
  )
  try:
    stdout_text, _ = proc.communicate(timeout=timeout)
  except subprocess.TimeoutExpired:
    proc.kill()
    stdout_text, _ = proc.communicate()
    print(f'[run] Process timed out after {timeout}s; stdout:\n{stdout_text}', flush=True)

  file_text = log_file.read_text(errors='replace') if log_file.exists() else ''
  combined = (stdout_text or '') + '\n' + file_text
  print(f'[run] Log output ({binary.name}, rc={proc.returncode}):\n{file_text.strip()}', flush=True)
  return combined


def _extract_url_credentials(raw_url: str) -> tuple[str, str | None, str | None]:
  """Split optional user[:pass]@ from an HTTP(S) URL, preserving emails with '@'."""
  m = re.match(r'^(https?://)([^/]+)(/.*)?$', raw_url.strip())
  if not m:
    return raw_url.strip(), None, None
  scheme, authority, rest = m.group(1), m.group(2), m.group(3) or ''
  if '@' not in authority:
    return f'{scheme}{authority}{rest}', None, None
  userinfo, host = authority.rsplit('@', 1)
  clean_url = f'{scheme}{host}{rest}'
  if ':' in userinfo:
    user, pwd = userinfo.split(':', 1)
    return clean_url, urllib.parse.unquote(user), urllib.parse.unquote(pwd)
  return clean_url, 'oauth2', urllib.parse.unquote(userinfo)


def clone_cli_repo(
    git_url: str,
    ref: str,
    dest: Path,
    token: str | None,
) -> Path:
  if dest.exists():
    shutil.rmtree(dest)

  clean_url, url_user, url_pwd = _extract_url_credentials(git_url)
  auth_user: str | None = url_user
  auth_pwd: str | None = url_pwd

  tok = (token or '').strip()
  if tok:
    if tok.startswith(('http://', 'https://')):
      _, tok_user, tok_pwd = _extract_url_credentials(tok)
      if tok_pwd:
        auth_user, auth_pwd = tok_user, tok_pwd
      else:
        raise RuntimeError(
            'LOONGBIRD_CLI_GIT_TOKEN appears to be a URL without embedded credentials. '
            'Please set LOONGBIRD_CLI_GIT_TOKEN to a GitCode/GitHub Personal Access Token '
            '(or "username:token").'
        )
    elif ':' in tok:
      auth_user, auth_pwd = tok.split(':', 1)
    else:
      auth_user, auth_pwd = 'oauth2', tok

  cmd = ['git', '-c', 'credential.helper=']
  b64_auth: str | None = None
  if auth_pwd:
    b64_auth = base64.b64encode(f'{auth_user or "oauth2"}:{auth_pwd}'.encode()).decode()
    cmd += ['-c', f'http.extraHeader=Authorization: Basic {b64_auth}']
  cmd += ['clone', '--depth', '1', '--branch', ref, clean_url, str(dest)]

  clone_env = dict(os.environ)
  clone_env['GIT_TERMINAL_PROMPT'] = '0'
  print(
      f'[e2e] Cloning Loongbird CLI ({ref}) into {dest} '
      f'(auth={"basic" if b64_auth else "none"})...',
      flush=True,
  )
  res = subprocess.run(
      cmd,
      env=clone_env,
      capture_output=True,
      text=True,
      timeout=300,
      check=False,
  )
  if res.returncode != 0:
    err = (res.stderr or '') + '\n' + (res.stdout or '')
    for secret in (tok, auth_pwd, b64_auth):
      if secret:
        err = err.replace(secret, '***')
    raise RuntimeError(f'git clone of Loongbird CLI failed (rc={res.returncode}):\n{err}')
  return dest


def main() -> int:
  parser = argparse.ArgumentParser(description='Loongbird Linux/Windows Desktop E2E Runner')
  parser.add_argument('--platform', choices=('linux', 'windows'), required=True)
  parser.add_argument('--shorebird-bin', type=Path, default=None)
  parser.add_argument('--cli-git-url', default=None, help='Git URL of loongbird CLI repo to clone if --shorebird-bin is omitted')
  parser.add_argument('--cli-ref', default='loongbird_3.44.9', help='Branch/tag to clone for --cli-git-url')
  parser.add_argument('--gateway-url', required=True)
  parser.add_argument('--token', required=True)
  parser.add_argument('--app-id', required=True)
  parser.add_argument('--engine-rev', required=True)
  parser.add_argument('--release-version', required=True, help='e.g. 10.0.1+1')
  parser.add_argument('--work-dir', type=Path, default=None)
  args = parser.parse_args()

  work_dir = args.work_dir or Path(tempfile.mkdtemp(prefix=f'loongbird_e2e_{args.platform}_'))
  work_dir.mkdir(parents=True, exist_ok=True)
  print(f'[e2e] platform={args.platform} version={args.release_version} work_dir={work_dir}')

  shorebird_bin = args.shorebird_bin
  if shorebird_bin is None:
    if not args.cli_git_url:
      raise RuntimeError('Either --shorebird-bin or --cli-git-url must be provided')
    cli_dir = clone_cli_repo(
        args.cli_git_url,
        args.cli_ref,
        work_dir / 'loongbird_cli',
        os.environ.get('LOONGBIRD_CLI_GIT_TOKEN'),
    )
    shorebird_bin = cli_dir / 'bin' / ('shorebird.bat' if args.platform == 'windows' else 'shorebird')

  gateway = args.gateway_url.rstrip('/')
  env = dict(os.environ)
  env['FLUTTER_STORAGE_BASE_URL'] = gateway
  env['SHOREBIRD_HOSTED_URL'] = gateway
  env['SHOREBIRD_TOKEN'] = args.token
  env['CI'] = 'true'

  shorebird_cmd = [str(shorebird_bin.resolve())]
  if args.platform == 'windows' and shorebird_bin.suffix.lower() in ('.bat', '.cmd'):
    shorebird_cmd = ['cmd', '/c', str(shorebird_bin.resolve())]

  # 1. Verify self-hosted bootstrap
  ver_res = run_cmd(shorebird_cmd + ['--version'], env=env, timeout=900)
  combined_ver = ver_res.stdout + '\n' + ver_res.stderr
  assert_no_upstream_shorebird_urls(combined_ver, 'bootstrap')
  if args.engine_rev[:10] not in combined_ver:
    raise RuntimeError(
        f'Expected engine revision {args.engine_rev} in shorebird --version output:\n{combined_ver}'
    )
  print('[e2e] Step 1 PASS: self-hosted CLI bootstrap verified')

  # Locate cached flutter binary inside loongbird/bin/cache/flutter/<rev>/bin/flutter
  loongbird_root = shorebird_bin.resolve().parent.parent
  flutter_cache_root = loongbird_root / 'bin' / 'cache' / 'flutter'
  flutter_bins = list(flutter_cache_root.glob('*/bin/flutter.bat' if args.platform == 'windows' else '*/bin/flutter'))
  if not flutter_bins:
    raise RuntimeError(f'Could not locate cached flutter under {flutter_cache_root}')
  flutter_bin = flutter_bins[0]
  flutter_cmd = ['cmd', '/c', str(flutter_bin)] if args.platform == 'windows' else [str(flutter_bin)]

  # 2. Create isolated desktop Flutter app
  app_dir = work_dir / 'e2e_app'
  if app_dir.exists():
    shutil.rmtree(app_dir)
  run_cmd(
      flutter_cmd + ['create', '--platforms=' + args.platform, '--project-name=e2e_desktop_app', str(app_dir)],
      env=env,
      timeout=300,
  )

  pubspec_path = app_dir / 'pubspec.yaml'
  pubspec = pubspec_path.read_text()
  pubspec = re.sub(r'^version:\s*.*$', f'version: {args.release_version}', pubspec, flags=re.MULTILINE)
  if 'shorebird_code_push:' not in pubspec:
    pubspec = pubspec.replace(
        '  flutter:\n    sdk: flutter\n',
        '  flutter:\n    sdk: flutter\n  shorebird_code_push: ^2.0.0\n',
        1,
    )
  if 'shorebird.yaml' not in pubspec:
    if 'uses-material-design: true' in pubspec:
      pubspec = pubspec.replace(
          '  uses-material-design: true\n',
          '  uses-material-design: true\n  assets:\n    - shorebird.yaml\n',
          1,
      )
    else:
      pubspec += '\nflutter:\n  assets:\n    - shorebird.yaml\n'
  pubspec_path.write_text(pubspec)

  (app_dir / 'shorebird.yaml').write_text(
      f'app_id: {args.app_id}\nbase_url: {gateway}\nauto_update: false\n'
  )
  (app_dir / 'lib' / 'main.dart').write_text(MAIN_DART)
  (app_dir / 'lib' / 'patch_workload.dart').write_text(PATCH_WORKLOAD_BASE)

  run_cmd(flutter_cmd + ['pub', 'get'], cwd=app_dir, env=env, timeout=300)

  # 3. Generate ephemeral RSA signing keys & run zero-local-engine release
  key_dir = work_dir / 'keys'
  key_dir.mkdir(exist_ok=True)
  priv_key, pub_key = generate_rsa_keypair(key_dir)

  rel_res = run_cmd(
      shorebird_cmd + [
          'release',
          f'--platforms={args.platform}',
          f'--public-key-path={pub_key}',
          '--no-confirm',
          '-v',
      ],
      cwd=app_dir,
      env=env,
      timeout=1800,
  )
  assert_no_upstream_shorebird_urls(rel_res.stdout + '\n' + rel_res.stderr, 'release')
  print('[e2e] Step 3 PASS: zero-local-engine desktop release published')

  if args.platform == 'linux':
    bundle_src = app_dir / 'build' / 'linux' / 'x64' / 'release' / 'bundle'
    exe_rel = Path('e2e_desktop_app')
  else:
    bundle_src = app_dir / 'build' / 'windows' / 'x64' / 'runner' / 'Release'
    exe_rel = Path('e2e_desktop_app.exe')

  base_bundle = work_dir / 'base_bundle'
  if base_bundle.exists():
    shutil.rmtree(base_bundle)
  shutil.copytree(bundle_src, base_bundle, symlinks=True)
  base_exe = base_bundle / exe_rel
  if not base_exe.is_file():
    raise RuntimeError(f'Released executable not found at {base_exe}')

  # 4. Mutate workload & publish RSA-signed Patch 1
  (app_dir / 'lib' / 'patch_workload.dart').write_text(PATCH_WORKLOAD_PATCH1)
  patch_res = run_cmd(
      shorebird_cmd + [
          'patch',
          f'--platforms={args.platform}',
          f'--release-version={args.release_version}',
          f'--private-key-path={priv_key}',
          f'--public-key-path={pub_key}',
          '--no-confirm',
          '-v',
      ],
      cwd=app_dir,
      env=env,
      timeout=1800,
  )
  assert_no_upstream_shorebird_urls(patch_res.stdout + '\n' + patch_res.stderr, 'patch')
  print('[e2e] Step 4 PASS: RSA-signed desktop patch published')

  # 5. Clean updater cache dir before 3-run verification
  if args.platform == 'linux':
    cache_dir = Path.home() / '.shorebird_cache'
    if cache_dir.exists():
      shutil.rmtree(cache_dir)
  else:
    local_app_data = Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData' / 'Local')))
    for p in ('updater_state.json', 'patches'):
      target = local_app_data / p
      if target.is_dir():
        shutil.rmtree(target)
      elif target.is_file():
        target.unlink()

  # Run 1: Boot BASE, download & verify Patch 1
  run1_env = dict(env)
  run1_env['LOONGBIRD_E2E_DO_UPDATE'] = '1'
  run1_out = launch_desktop_app(args.platform, base_exe, work_dir / 'run1.log', run1_env, timeout=120)
  if 'marker=BASE patch=0 passed=6/6' not in run1_out:
    raise RuntimeError(f'Run 1 failed base assertion:\n{run1_out}')
  if 'UPDATE_DONE nextPatch=1' not in run1_out:
    raise RuntimeError(f'Run 1 did not download Patch 1:\n{run1_out}')
  print('[e2e] Run 1 PASS: BASE booted (6/6) & Patch 1 downloaded/verified')

  # Run 2: Boot Patch 1
  run2_env = dict(env)
  run2_env['LOONGBIRD_E2E_DO_UPDATE'] = '0'
  run2_out = launch_desktop_app(args.platform, base_exe, work_dir / 'run2.log', run2_env, timeout=90)
  if 'marker=PATCH1 patch=1 passed=6/6' not in run2_out:
    raise RuntimeError(f'Run 2 failed Patch 1 assertion:\n{run2_out}')
  print('[e2e] Run 2 PASS: Patch 1 active (marker=PATCH1, patch=1, 6/6, r1=15150)')

  # Run 3: Confirm zero rollback
  run3_out = launch_desktop_app(args.platform, base_exe, work_dir / 'run3.log', run2_env, timeout=90)
  if 'marker=PATCH1 patch=1 passed=6/6' not in run3_out:
    raise RuntimeError(f'Run 3 failed persistence/no-rollback assertion:\n{run3_out}')
  print('[e2e] Run 3 PASS: Patch 1 persisted with zero rollback!')
  print(f'[e2e] ALL CHECKS PASSED for {args.platform} ({args.release_version})')
  return 0


if __name__ == '__main__':
  sys.exit(main())
