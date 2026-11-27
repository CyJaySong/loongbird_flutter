#!/usr/bin/env python3
"""Pure-engine functional verification for Loongbird Linux x64 & Windows x64 builds.

Verifies every engine deliverable produced by `loongbird_flutter` with ZERO
dependency on the Loongbird CLI or external self-hosted gateway:

  Stage 1 — Dart SDK (`dart-sdk-<os>-x64.zip`):
    * Zip integrity, null SDK hash (`0000000000`), `devtools` & `dartdoc` presence
    * `dart --version` (3.12.2)
    * JIT execution (`dartvm`)
    * `dart compile exe` + native AOT executable run
    * `dart compile aot-snapshot` + `dartaotruntime` run

  Stage 2 — Host Debug Tools & Android 3-ABI Cross-Arch `gen_snapshot`:
    * `<os>-x64/artifacts.zip` (`gen_snapshot`, `flutter_tester`, `impellerc`, `icudtl.dat`)
    * Compile AOT kernel `.dill` with our `frontend_server_aot.dart.snapshot`
    * Compile real ARM64 / ARM32 / X64 AOT ELF libraries using all 3 Android
      `android-{arm64,arm,x64}-release/<os>-x64.zip` `gen_snapshot` binaries and
      verify ELF magic + exact `e_machine` (`EM_AARCH64=183`, `EM_ARM=40`, `EM_X86_64=62`)

  Stage 3 — Rust `patch` Binary Diff CLI (`patch-<os>-x64.zip`):
    * Synthetic 64 KiB binary diff generation

  Stage 4 — Full Flutter Desktop Release Build + 5-Run Live RSA-2048 Strict Hot-Update:
    * Local in-process Artifact Proxy on `127.0.0.1` serves our built zips locally
      and 302-redirects untouched upstream assets (fonts, sky_engine, etc.) directly
      to `https://storage.googleapis.com` for the upstream Flutter 3.44.9 commit
    * Local in-process Mock Updater Server on `127.0.0.1` speaks the exact
      Shorebird Rust updater protocol (`/api/v1/patches/check`, `/patches/1.bin`,
      `/api/v1/patches/events`)
    * Builds a real Flutter desktop Release app (BASE) + patched Release (PATCH1)
    * Generates real binary diff with our Rust `patch` tool and signs the
      uncompressed Patch 1 SHA-256 with a fresh RSA-2048 key (`patch_verification: strict`)
    * Executes 5 consecutive launches of the compiled Release desktop binary:
        Run 1: Boots BASE, runs 6/6 Dart stress tests, checks/downloads/verifies
               RSA signature, inflates Patch 1, promotes for next boot
        Run 2: Cold boots directly into Patch 1 (strict boot-time RSA re-verify),
               verifies `marker=PATCH1`, `patch=1`, 6/6 stress tests pass, `upToDate`
        Run 3: Cold re-boots again — confirms Patch 1 stays active with zero rollback
        Run 4: Offers Patch 2 signed with the wrong key; the update must be refused
               (behavioural proof that RSA verification is enforced, not just wired)
        Run 5: Confirms the refused Update left Patch 1 running untouched
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import posixpath
import secrets
import shutil
import socketserver
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

UPSTREAM_FLUTTER_3449_REV = "5a2a6a42cce67f965cf540fcecf616faca624aa1"
UPSTREAM_STORAGE_URL = "https://storage.googleapis.com"

SMOKE_DART = r'''import 'dart:async';
import 'dart:convert';
import 'dart:isolate';
import 'dart:typed_data';

Future<void> main() async {
  var sum = 0;
  for (var i = 1; i <= 200; i++) {
    sum += i;
  }
  final isoVal = await Isolate.run(() => 42 * 2);
  var caught = 'none';
  try {
    throw FormatException('expected');
  } on FormatException {
    caught = 'caught';
  }
  final arr = Int64List.fromList(<int>[sum, isoVal]);
  final decoded = jsonDecode(jsonEncode({'sum': arr[0], 'iso': arr[1]})) as Map;
  final re = RegExp(r'^(\d+):(\d+)$');
  final m = re.firstMatch('${decoded['sum']}:${decoded['iso']}');
  if (m == null) {
    throw StateError('regex failed');
  }
  print('SMOKE_OK:${m.group(1)}:${m.group(2)}:$caught');
}
'''

MAIN_DART = r'''import 'dart:convert';
import 'dart:ffi' as ffi;
import 'dart:io';
import 'dart:isolate';
import 'package:flutter/material.dart';
import 'patch_workload.dart';

final class _UpdateResult extends ffi.Struct {
  @ffi.Int32()
  external int status;

  external ffi.Pointer<ffi.Uint8> message;
}

typedef _PatchNumNative = ffi.UintPtr Function();
typedef _PatchNumDart = int Function();
typedef _CheckNative = ffi.Bool Function(ffi.Pointer<ffi.Uint8>);
typedef _CheckDart = bool Function(ffi.Pointer<ffi.Uint8>);
typedef _UpdateNative = ffi.Pointer<_UpdateResult> Function(ffi.Pointer<ffi.Uint8>);
typedef _UpdateDart = ffi.Pointer<_UpdateResult> Function(ffi.Pointer<ffi.Uint8>);
typedef _FreeNative = ffi.Void Function(ffi.Pointer<_UpdateResult>);
typedef _FreeDart = void Function(ffi.Pointer<_UpdateResult>);

ffi.DynamicLibrary _openEngineLib() {
  if (Platform.isWindows) {
    return ffi.DynamicLibrary.open('flutter_windows.dll');
  }
  final proc = ffi.DynamicLibrary.process();
  if (proc.providesSymbol('shorebird_current_boot_patch_number')) {
    return proc;
  }
  return ffi.DynamicLibrary.open('libflutter_linux_gtk.so');
}

String _readCString(ffi.Pointer<ffi.Uint8> ptr) {
  if (ptr == ffi.nullptr) return '';
  final bytes = <int>[];
  var offset = 0;
  while (true) {
    final b = ptr[offset];
    if (b == 0) break;
    bytes.add(b);
    offset++;
    if (offset > 8192) break;
  }
  return utf8.decode(bytes, allowMalformed: true);
}

int _currentPatchNumber() {
  final lib = _openEngineLib();
  final fn = lib.lookupFunction<_PatchNumNative, _PatchNumDart>(
    'shorebird_current_boot_patch_number',
  );
  return fn();
}

int _nextPatchNumber() {
  final lib = _openEngineLib();
  final fn = lib.lookupFunction<_PatchNumNative, _PatchNumDart>(
    'shorebird_next_boot_patch_number',
  );
  return fn();
}

bool _checkForDownloadableUpdate() {
  final lib = _openEngineLib();
  final fn = lib.lookupFunction<_CheckNative, _CheckDart>(
    'shorebird_check_for_downloadable_update',
  );
  return fn(ffi.nullptr);
}

(int, String) _runNativeUpdate() {
  final lib = _openEngineLib();
  final updateFn = lib.lookupFunction<_UpdateNative, _UpdateDart>(
    'shorebird_update_with_result',
  );
  final freeFn = lib.lookupFunction<_FreeNative, _FreeDart>(
    'shorebird_free_update_result',
  );
  final resPtr = updateFn(ffi.nullptr);
  if (resPtr == ffi.nullptr) {
    return (-1, 'null UpdateResult');
  }
  try {
    final status = resPtr.ref.status;
    final msg = _readCString(resPtr.ref.message);
    return (status, msg);
  } finally {
    freeFn(resPtr);
  }
}

void _emit(String msg) {
  // ignore: avoid_print
  print(msg);
  try {
    stdout.writeln(msg);
  } catch (_) {}
  final logPath = Platform.environment['LOONGBIRD_VERIFY_LOG'];
  if (logPath != null && logPath.isNotEmpty) {
    File(logPath).writeAsStringSync('$msg\n', mode: FileMode.append, flush: true);
  }
}

void main() {
  _emit('[ENGINE-VERIFY] BOOT marker=$workloadMarker');
  runApp(const EngineVerifyApp());
}

class EngineVerifyApp extends StatefulWidget {
  const EngineVerifyApp({super.key});

  @override
  State<EngineVerifyApp> createState() => _EngineVerifyAppState();
}

class _EngineVerifyAppState extends State<EngineVerifyApp> {
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addPostFrameCallback((_) => _run());
  }

  Future<void> _run() async {
    try {
      final current = await Isolate.run(_currentPatchNumber);
      final results = await runAllStressTests();
      final passed = results.values.where((v) => v.startsWith('PASS')).length;
      final total = results.length;
      final detail = results.entries.map((e) => '${e.key}=${e.value}').join(' | ');
      _emit(
        '[ENGINE-VERIFY] STRESS_RESULT marker=$workloadMarker '
        'available=true patch=$current '
        'passed=$passed/$total $detail',
      );

      final downloadable = await Isolate.run(_checkForDownloadableUpdate);
      _emit('[ENGINE-VERIFY] CHECK_BEFORE downloadable=$downloadable');

      if (Platform.environment['LOONGBIRD_VERIFY_DO_UPDATE'] == '1') {
        _emit('[ENGINE-VERIFY] UPDATE_START');
        final (status, msg) = await Isolate.run(_runNativeUpdate);
        _emit('[ENGINE-VERIFY] UPDATE_RESULT status=$status msg=$msg');
        var next = 0;
        for (var i = 0; i < 30; i++) {
          next = await Isolate.run(_nextPatchNumber);
          if (next > 0) break;
          await Future<void>.delayed(const Duration(milliseconds: 500));
        }
        _emit('[ENGINE-VERIFY] UPDATE_DONE nextPatch=$next');
      } else {
        await Future<void>.delayed(const Duration(seconds: 1));
      }
    } catch (e, st) {
      _emit('[ENGINE-VERIFY] FATAL err=$e\n$st');
    } finally {
      _emit('[ENGINE-VERIFY] EXITING');
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

WORKLOAD_BASE = r'''const String workloadMarker = 'BASE';

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

WORKLOAD_PATCH1 = r'''const String workloadMarker = 'PATCH1';

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


def _safe_print(text: str) -> None:
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "ascii"
        sys.stdout.write(text.encode(enc, errors="replace").decode(enc, errors="replace"))
        sys.stdout.flush()


def _kill_process_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        try:
            proc.kill()
        except OSError:
            pass


def run_cmd(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 600,
    check: bool = True,
    stream: bool = True,
) -> tuple[int, str]:
    _safe_print(f"$ {' '.join(cmd)}\n")
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    chunks: list[str] = []

    def _reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            chunks.append(line)
            if stream:
                _safe_print(line)

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _safe_print(f"[verify] TIMEOUT after {timeout}s — killing PID {proc.pid}\n")
        _kill_process_tree(proc)
        t.join(timeout=5)
        raise RuntimeError(f"Command timed out ({timeout}s): {' '.join(cmd)}")
    t.join(timeout=10)
    out = "".join(chunks)
    if check and rc != 0:
        if not stream and out:
            _safe_print(out)
        raise RuntimeError(f"Command failed (rc={rc}): {' '.join(cmd)}")
    return rc, out


def extract_zip_preserving_mode(zip_path: Path, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        bad = zf.testzip()
        if bad is not None:
            raise RuntimeError(f"Corrupted zip entry '{bad}' in {zip_path}")
        for info in zf.infolist():
            rel = Path(info.filename)
            if rel.is_absolute() or ".." in rel.parts:
                raise RuntimeError(f"Unsafe zip member: {info.filename}")
            target = dest_dir / rel
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            unix_mode = (info.external_attr >> 16) & 0xFFFF
            if os.name != "nt" and stat.S_ISLNK(unix_mode):
                link_target = zf.read(info).decode("utf-8")
                if target.exists() or target.is_symlink():
                    target.unlink()
                os.symlink(link_target, target)
                continue
            with zf.open(info, "r") as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
            if os.name != "nt" and (unix_mode & 0o777):
                os.chmod(target, unix_mode & 0o777)


def find_openssl() -> str:
    found = shutil.which("openssl")
    if found:
        return found
    if os.name == "nt":
        for candidate in [
            r"C:\Program Files\Git\usr\bin\openssl.exe",
            r"C:\Program Files\Git\mingw64\bin\openssl.exe",
            r"C:\msys64\usr\bin\openssl.exe",
        ]:
            if Path(candidate).is_file():
                return candidate
    raise RuntimeError("openssl binary not found in PATH or standard Git-for-Windows locations")


def generate_rsa_keypair(key_dir: Path) -> tuple[Path, str]:
    """Generate an RSA-2048 keypair and return (private_pem, pkcs1_der_pub_b64)."""
    openssl = find_openssl()
    priv_pem = key_dir / "private.pem"
    pub_pem = key_dir / "public.pem"
    pub_der = key_dir / "public_pkcs1.der"
    run_cmd([openssl, "genrsa", "-out", str(priv_pem), "2048"], stream=False)
    run_cmd([openssl, "rsa", "-in", str(priv_pem), "-pubout", "-out", str(pub_pem)], stream=False)
    run_cmd(
        [
            openssl,
            "rsa",
            "-pubin",
            "-in",
            str(pub_pem),
            "-inform",
            "PEM",
            "-RSAPublicKey_out",
            "-outform",
            "DER",
            "-out",
            str(pub_der),
        ],
        stream=False,
    )
    pub_b64 = base64.b64encode(pub_der.read_bytes()).decode("ascii")
    return priv_pem, pub_b64


def sign_sha256_hex(private_pem: Path, sha256_hex: str) -> str:
    """Sign the UTF-8 hex SHA-256 string with RSA PKCS#1 v1.5 + SHA-256."""
    openssl = find_openssl()
    proc = subprocess.run(
        [openssl, "dgst", "-sha256", "-sign", str(private_pem)],
        input=sha256_hex.encode("ascii"),
        capture_output=True,
        check=True,
    )
    return base64.b64encode(proc.stdout).decode("ascii")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


class LocalArtifactProxy:
    """Serves locally staged engine zips and 302-redirects untouched assets upstream."""

    def __init__(
        self,
        artifacts_dir: Path,
        engine_rev: str,
        upstream_rev: str,
        upstream_base_url: str,
    ) -> None:
        self.artifacts_dir = artifacts_dir.resolve()
        self.engine_rev = engine_rev
        self.upstream_rev = upstream_rev
        self.upstream_base_url = upstream_base_url.rstrip("/")
        self.local_hits: dict[str, int] = {}
        self.redirect_hits: list[str] = []
        self._lock = threading.Lock()
        self._server: socketserver.ThreadingTCPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def start(self) -> None:
        proxy = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args) -> None:
                pass

            def _resolve(self) -> tuple[str, Path | None]:
                raw_path = self.path.split("?", 1)[0]
                norm = posixpath.normpath(raw_path).lstrip("/")
                if norm.startswith("..") or "/../" in norm:
                    return norm, None
                candidate = (proxy.artifacts_dir / norm).resolve()
                try:
                    candidate.relative_to(proxy.artifacts_dir)
                except ValueError:
                    return norm, None
                if candidate.is_file():
                    return norm, candidate
                return norm, None

            def _handle(self, send_body: bool) -> None:
                norm, local_file = self._resolve()
                if local_file is not None:
                    size = local_file.stat().st_size
                    start = 0
                    end = size - 1
                    status = 200
                    range_hdr = self.headers.get("Range")
                    if range_hdr and range_hdr.startswith("bytes="):
                        spec = range_hdr[len("bytes="):].split(",", 1)[0].strip()
                        s_str, _, e_str = spec.partition("-")
                        if s_str:
                            start = int(s_str)
                            end = int(e_str) if e_str else size - 1
                        elif e_str:
                            suffix = int(e_str)
                            start = max(0, size - suffix)
                            end = size - 1
                        if start >= size or start > end:
                            self.send_response(416)
                            self.send_header("Content-Range", f"bytes */{size}")
                            self.send_header("Content-Length", "0")
                            self.end_headers()
                            return
                        end = min(end, size - 1)
                        status = 206

                    length = end - start + 1
                    self.close_connection = True
                    self.send_response(status)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Length", str(length))
                    self.send_header("Connection", "close")
                    if status == 206:
                        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                    self.end_headers()
                    with proxy._lock:
                        proxy.local_hits[norm] = proxy.local_hits.get(norm, 0) + 1
                    _safe_print(f"[artifact-proxy] LOCAL {status} /{norm} ({length} B)\n")
                    if send_body:
                        with local_file.open("rb") as f:
                            f.seek(start)
                            remaining = length
                            while remaining > 0:
                                chunk = f.read(min(1024 * 1024, remaining))
                                if not chunk:
                                    break
                                self.wfile.write(chunk)
                                remaining -= len(chunk)
                    return

                rewritten = norm.replace(proxy.engine_rev, proxy.upstream_rev)
                target = f"{proxy.upstream_base_url}/{rewritten}"
                with proxy._lock:
                    proxy.redirect_hits.append(norm)
                _safe_print(f"[artifact-proxy] 302 /{norm} -> {target}\n")
                self.close_connection = True
                self.send_response(302)
                self.send_header("Location", target)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()

            def do_GET(self) -> None:
                self._handle(send_body=True)

            def do_HEAD(self) -> None:
                self._handle(send_body=False)

        socketserver.ThreadingTCPServer.allow_reuse_address = True
        self._server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


class MockUpdaterServer:
    """In-process Shorebird updater mock speaking /api/v1/patches/{check,events}."""

    def __init__(self, expected_app_id: str, expected_platform: str) -> None:
        self.expected_app_id = expected_app_id
        self.expected_platform = expected_platform
        self.patch_bytes: bytes | None = None
        self.patch_hash: str | None = None
        self.patch_signature: str | None = None
        self.patch_number: int = 1
        self.check_requests: list[dict] = []
        self.download_count = 0
        self.events: list[dict] = []
        self._lock = threading.Lock()
        self._server: socketserver.ThreadingTCPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        assert self._server is not None
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def set_patch(
        self,
        patch_bytes: bytes,
        patch_hash: str,
        patch_signature: str,
        number: int = 1,
    ) -> None:
        with self._lock:
            self.patch_bytes = patch_bytes
            self.patch_hash = patch_hash
            self.patch_signature = patch_signature
            self.patch_number = number

    def start(self) -> None:
        mock = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args) -> None:
                pass

            def _send_json(self, code: int, payload: dict) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.close_connection = True
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length > 0 else b"{}"
                payload = json.loads(raw.decode("utf-8"))
                path = self.path.split("?", 1)[0]

                if path == "/api/v1/patches/check":
                    with mock._lock:
                        mock.check_requests.append(payload)
                        patch_bytes = mock.patch_bytes
                        patch_hash = mock.patch_hash
                        patch_sig = mock.patch_signature
                        patch_number = mock.patch_number
                    cur = payload.get("current_patch_number")
                    _safe_print(
                        f"[mock-updater] POST /api/v1/patches/check "
                        f"app={payload.get('app_id')} ver={payload.get('release_version')} "
                        f"platform={payload.get('platform')} arch={payload.get('arch')} "
                        f"current_patch={cur}\n"
                    )
                    if patch_bytes is not None and (
                        cur is None or int(cur) < patch_number
                    ):
                        self._send_json(
                            200,
                            {
                                "patch_available": True,
                                "patch": {
                                    "number": patch_number,
                                    "hash": patch_hash,
                                    "download_url": (
                                        f"{mock.base_url}/patches/{patch_number}.bin"
                                    ),
                                    "hash_signature": patch_sig,
                                },
                            },
                        )
                    else:
                        self._send_json(200, {"patch_available": False})
                    return

                if path == "/api/v1/patches/events":
                    with mock._lock:
                        mock.events.append(payload)
                    _safe_print(f"[mock-updater] POST /api/v1/patches/events {payload}\n")
                    self._send_json(201, {"ok": True})
                    return

                self._send_json(404, {"error": f"unknown path: {path}"})

            def do_GET(self) -> None:
                path = self.path.split("?", 1)[0]
                if path.startswith("/patches/") and path.endswith(".bin"):
                    with mock._lock:
                        data = mock.patch_bytes
                        if data is not None:
                            mock.download_count += 1
                    if data is None:
                        self._send_json(404, {"error": "patch not configured"})
                        return
                    _safe_print(f"[mock-updater] GET {path} ({len(data)} B)\n")
                    self.close_connection = True
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self._send_json(404, {"error": f"unknown path: {path}"})

        socketserver.ThreadingTCPServer.allow_reuse_address = True
        self._server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


def verify_stage1_dart_sdk(
    artifacts_dir: Path, engine_rev: str, platform: str, work: Path
) -> Path:
    _safe_print("\n=== STAGE 1: Dart SDK Functional Verification ===\n")
    sdk_zip = artifacts_dir / f"flutter_infra_release/flutter/{engine_rev}/dart-sdk-{platform}-x64.zip"
    if not sdk_zip.is_file():
        raise RuntimeError(f"Missing Dart SDK zip: {sdk_zip}")

    extract_root = work / "stage1_sdk"
    extract_zip_preserving_mode(sdk_zip, extract_root)
    sdk_dir = extract_root / "dart-sdk"
    exe = ".exe" if platform == "windows" else ""
    dart_bin = sdk_dir / "bin" / f"dart{exe}"
    dartvm_bin = sdk_dir / "bin" / f"dartvm{exe}"
    aot_bin = sdk_dir / "bin" / f"dartaotruntime{exe}"

    for p in [
        dart_bin,
        dartvm_bin,
        aot_bin,
        sdk_dir / "bin" / "resources" / "devtools",
        sdk_dir / "bin" / "resources" / "dartdoc",
    ]:
        if not p.exists():
            raise RuntimeError(f"Expected Dart SDK path missing: {p}")

    # Verify null SDK hash (`0000000000`) so Dart VM accepts upstream/patched kernel binaries.
    dartvm_bytes = dartvm_bin.read_bytes()
    null_hash_hits = dartvm_bytes.count(b"0000000000")
    if null_hash_hits == 0:
        raise RuntimeError("dartvm binary does not contain expected null SDK hash 0000000000")
    _safe_print(f"[stage1] dartvm null SDK hash hits: {null_hash_hits}\n")

    _, ver_out = run_cmd([str(dart_bin), "--version"])
    if "3.12.2" not in ver_out:
        raise RuntimeError(f"Expected Dart 3.12.2 in version output, got: {ver_out}")

    smoke_dir = work / "stage1_smoke"
    smoke_dir.mkdir(parents=True, exist_ok=True)
    smoke_dart = smoke_dir / "smoke.dart"
    smoke_dart.write_text(SMOKE_DART, encoding="utf-8")

    # 1a. JIT via dartvm
    _, jit_out = run_cmd([str(dartvm_bin), str(smoke_dart)])
    if "SMOKE_OK:20100:84:caught" not in jit_out:
        raise RuntimeError(f"JIT smoke test failed: {jit_out}")

    # 1b. AOT native executable via `dart compile exe`
    smoke_exe = smoke_dir / f"smoke_aot{exe}"
    run_cmd([str(dart_bin), "compile", "exe", str(smoke_dart), "-o", str(smoke_exe)])
    _, exe_out = run_cmd([str(smoke_exe)])
    if "SMOKE_OK:20100:84:caught" not in exe_out:
        raise RuntimeError(f"AOT exe smoke test failed: {exe_out}")

    # 1c. AOT snapshot via `dart compile aot-snapshot` + `dartaotruntime`
    smoke_aot = smoke_dir / "smoke.aot"
    run_cmd([str(dart_bin), "compile", "aot-snapshot", str(smoke_dart), "-o", str(smoke_aot)])
    _, aot_out = run_cmd([str(aot_bin), str(smoke_aot)])
    if "SMOKE_OK:20100:84:caught" not in aot_out:
        raise RuntimeError(f"dartaotruntime smoke test failed: {aot_out}")

    _safe_print("[stage1] PASS: Dart SDK JIT + AOT exe + dartaotruntime + devtools/dartdoc\n")
    return sdk_dir


def verify_stage2_tools_and_android_gen_snapshot(
    artifacts_dir: Path, engine_rev: str, platform: str, work: Path, sdk_dir: Path
) -> None:
    _safe_print("\n=== STAGE 2: Host Debug Tools & Android 3-ABI gen_snapshot Verification ===\n")
    exe = ".exe" if platform == "windows" else ""

    # 2a. Host debug artifacts.zip
    host_dbg_zip = artifacts_dir / f"flutter_infra_release/flutter/{engine_rev}/{platform}-x64/artifacts.zip"
    if not host_dbg_zip.is_file():
        raise RuntimeError(f"Missing host debug zip: {host_dbg_zip}")
    host_dbg_dir = work / "stage2_host_debug"
    extract_zip_preserving_mode(host_dbg_zip, host_dbg_dir)
    for req in [f"gen_snapshot{exe}", f"flutter_tester{exe}", f"impellerc{exe}", "icudtl.dat"]:
        if not (host_dbg_dir / req).is_file():
            raise RuntimeError(f"Missing required host debug tool '{req}' in {host_dbg_zip}")
    run_cmd([str(host_dbg_dir / f"gen_snapshot{exe}"), "--version"])

    # 2b. Compile a real AOT kernel .dill with our frontend_server_aot.dart.snapshot
    dill_path = work / "stage2_smoke.dill"
    smoke_dart = work / "stage1_smoke" / "smoke.dart"
    run_cmd(
        [
            str(sdk_dir / "bin" / f"dartaotruntime{exe}"),
            str(sdk_dir / "bin" / "snapshots" / "frontend_server_aot.dart.snapshot"),
            f"--sdk-root={sdk_dir / 'lib' / '_internal'}",
            "--platform=vm_platform_strong.dill",
            "--target=vm",
            "--aot",
            "--tfa",
            "--no-support-mirrors",
            f"--output-dill={dill_path}",
            str(smoke_dart),
        ]
    )
    if not dill_path.is_file() or dill_path.stat().st_size == 0:
        raise RuntimeError("Failed to compile AOT kernel .dill for Android gen_snapshot check")

    # 2c. Verify all 3 Android cross-arch gen_snapshots produce valid ELF with exact e_machine
    expected_machines = {
        "arm64": (183, "EM_AARCH64"),
        "arm": (40, "EM_ARM"),
        "x64": (62, "EM_X86_64"),
    }
    for abi, (expected_e_machine, machine_name) in expected_machines.items():
        gs_zip = (
            artifacts_dir
            / f"flutter_infra_release/flutter/{engine_rev}/android-{abi}-release/{platform}-x64.zip"
        )
        if not gs_zip.is_file():
            raise RuntimeError(f"Missing Android {abi} gen_snapshot zip: {gs_zip}")
        abi_dir = work / f"stage2_android_{abi}"
        extract_zip_preserving_mode(gs_zip, abi_dir)
        gs_bin = abi_dir / f"gen_snapshot{exe}"
        if not gs_bin.is_file():
            raise RuntimeError(f"Missing gen_snapshot in {gs_zip}")
        run_cmd([str(gs_bin), "--version"])

        elf_out = abi_dir / f"libapp_{abi}.so"
        run_cmd(
            [
                str(gs_bin),
                "--deterministic",
                "--snapshot_kind=app-aot-elf",
                f"--elf={elf_out}",
                str(dill_path),
            ]
        )
        hdr = elf_out.read_bytes()[:20]
        if len(hdr) < 20 or hdr[:4] != b"\x7fELF":
            raise RuntimeError(f"Android {abi} output is not a valid ELF file: {elf_out}")
        e_machine = struct.unpack("<H", hdr[18:20])[0]
        if e_machine != expected_e_machine:
            raise RuntimeError(
                f"Android {abi} ELF e_machine mismatch: expected {expected_e_machine} "
                f"({machine_name}), got {e_machine}"
            )
        _safe_print(
            f"[stage2] android-{abi} ELF OK: {elf_out.stat().st_size} B, "
            f"e_machine={e_machine} ({machine_name})\n"
        )

    _safe_print("[stage2] PASS: Host debug tools & Android arm64/arm/x64 AOT ELF compilation\n")


def verify_stage3_patch_cli(
    artifacts_dir: Path, engine_rev: str, platform: str, work: Path
) -> Path:
    _safe_print("\n=== STAGE 3: Rust `patch` Binary Diff Tool Verification ===\n")
    exe = ".exe" if platform == "windows" else ""
    patch_zip = artifacts_dir / f"shorebird/{engine_rev}/patch-{platform}-x64.zip"
    if not patch_zip.is_file():
        raise RuntimeError(f"Missing patch CLI zip: {patch_zip}")

    patch_dir = work / "stage3_patch"
    extract_zip_preserving_mode(patch_zip, patch_dir)
    patch_bin = patch_dir / f"patch{exe}"
    if not patch_bin.is_file():
        raise RuntimeError(f"Missing patch binary inside {patch_zip}")
    if os.name != "nt":
        os.chmod(patch_bin, os.stat(patch_bin).st_mode | 0o111)

    base_blob = patch_dir / "base.bin"
    new_blob = patch_dir / "new.bin"
    diff_blob = patch_dir / "diff.bin"
    raw = bytearray(os.urandom(65536))
    base_blob.write_bytes(raw)
    raw[1024:2048] = os.urandom(1024)
    new_blob.write_bytes(raw)

    run_cmd([str(patch_bin), str(base_blob), str(new_blob), str(diff_blob)])
    if not diff_blob.is_file() or diff_blob.stat().st_size == 0:
        raise RuntimeError("patch CLI produced empty diff output")
    _safe_print(f"[stage3] PASS: patch CLI produced {diff_blob.stat().st_size} B binary diff\n")
    return patch_bin


def ensure_flutter_release_ref(flutter_root: Path) -> None:
    """Ensure refs/remotes/origin/flutter_release/3.44.9 exists so flutter --version reports 3.44.9."""
    res = subprocess.run(
        ["git", "rev-parse", "--verify", "refs/remotes/origin/flutter_release/3.44.9"],
        cwd=str(flutter_root),
        capture_output=True,
        text=True,
    )
    if res.returncode == 0:
        return
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=str(flutter_root), text=True
    ).strip()
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/flutter_release/3.44.9", head],
        cwd=str(flutter_root),
        check=True,
    )


def find_release_app_exe(bundle_dir: Path, platform: str) -> tuple[Path, Path]:
    if platform == "linux":
        exe = bundle_dir / "lb_engine_test"
        libapp = bundle_dir / "lib" / "libapp.so"
    else:
        exe = bundle_dir / "lb_engine_test.exe"
        libapp = bundle_dir / "data" / "app.so"
    if not exe.is_file():
        raise RuntimeError(f"Built executable not found: {exe}")
    if not libapp.is_file():
        raise RuntimeError(f"Built AOT library not found: {libapp}")
    return exe, libapp


def run_desktop_app(
    exe_path: Path,
    platform: str,
    run_label: str,
    log_path: Path,
    extra_env: dict[str, str],
) -> str:
    if log_path.exists():
        log_path.unlink()
    env = os.environ.copy()
    env.update(extra_env)
    env["LOONGBIRD_VERIFY_LOG"] = str(log_path)

    if platform == "linux":
        env["LIBGL_ALWAYS_SOFTWARE"] = "1"
        cmd = ["xvfb-run", "-a", "-s", "-screen 0 1280x720x24", str(exe_path)]
    else:
        cmd = [str(exe_path)]

    _safe_print(f"\n--- {run_label} ---\n")
    rc, stdout_text = run_cmd(cmd, cwd=exe_path.parent, env=env, timeout=180, check=False)
    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
    combined = (stdout_text + "\n" + log_text).strip()
    _safe_print(f"[{run_label}] exit_code={rc}\n")
    if log_text:
        _safe_print(f"[{run_label}] app log:\n{log_text}\n")
    if rc != 0:
        raise RuntimeError(f"{run_label} exited with rc={rc}\n{combined}")
    return combined


def verify_stage4_desktop_hot_update(
    flutter_root: Path,
    artifacts_dir: Path,
    engine_rev: str,
    upstream_rev: str,
    upstream_storage_url: str,
    platform: str,
    work: Path,
    patch_bin: Path,
) -> None:
    _safe_print("\n=== STAGE 4: Full Desktop Release Build + 3-Run RSA-2048 Strict Hot-Update ===\n")

    ensure_flutter_release_ref(flutter_root)

    # Clear cached engine artifacts inside flutter_root so Flutter is forced to fetch
    # our freshly built Dart SDK and release engine from the local artifact proxy.
    cache_dir = flutter_root / "bin" / "cache"
    if cache_dir.exists():
        shutil.rmtree(cache_dir, ignore_errors=True)

    app_id = f"engine-verify-{platform}-{secrets.token_hex(6)}"
    proxy = LocalArtifactProxy(artifacts_dir, engine_rev, upstream_rev, upstream_storage_url)
    mock = MockUpdaterServer(expected_app_id=app_id, expected_platform=platform)
    proxy.start()
    mock.start()

    win_cache_dir: Path | None = None
    if platform == "windows" and os.environ.get("LOCALAPPDATA"):
        win_cache_dir = Path(os.environ["LOCALAPPDATA"]) / "shorebird_updater" / app_id

    try:
        flutter_exe = str(flutter_root / "bin" / ("flutter.bat" if platform == "windows" else "flutter"))
        flutter_cmd = ["cmd", "/c", flutter_exe] if platform == "windows" else [flutter_exe]
        build_env = os.environ.copy()
        build_env["FLUTTER_STORAGE_BASE_URL"] = proxy.base_url
        build_env["CI"] = "true"
        # Ensure Flutter's pub uses public pub.dev cleanly on GitHub runners.
        build_env.pop("PUB_HOSTED_URL", None)

        run_cmd([*flutter_cmd, "--version", "--suppress-analytics"], env=build_env, timeout=600)
        # `flutter --version` prints the engine hash from the informational
        # engine_stamp.json (not produced by our build), so verify the engine
        # revision the tool actually resolves via bin/cache/engine.stamp instead.
        stamp_file = flutter_root / "bin" / "cache" / "engine.stamp"
        stamp = stamp_file.read_text(encoding="utf-8").strip() if stamp_file.is_file() else ""
        if stamp != engine_rev:
            raise RuntimeError(f"Expected bin/cache/engine.stamp={engine_rev}, got '{stamp}'")
        _safe_print(f"[stage4] bin/cache/engine.stamp verified: {stamp}\n")

        dart_sdk_rel = f"flutter_infra_release/flutter/{engine_rev}/dart-sdk-{platform}-x64.zip"
        if proxy.local_hits.get(dart_sdk_rel, 0) < 1:
            raise RuntimeError(f"Flutter did NOT fetch local Dart SDK {dart_sdk_rel} from proxy!")

        # Generate fresh RSA-2048 signing keypair
        key_dir = work / "stage4_keys"
        key_dir.mkdir(parents=True, exist_ok=True)
        priv_pem, pub_b64 = generate_rsa_keypair(key_dir)
        # flutter_tools only compiles `patch_public_key` into the bundled
        # shorebird.yaml from SHOREBIRD_PUBLIC_KEY (the yaml key is dropped).
        build_env["SHOREBIRD_PUBLIC_KEY"] = pub_b64

        # Create minimal test Flutter desktop app
        app_dir = work / "lb_engine_test"
        run_cmd(
            [
                *flutter_cmd,
                "create",
                "--platforms",
                platform,
                "--project-name",
                "lb_engine_test",
                str(app_dir),
            ],
            env=build_env,
            timeout=300,
        )

        # Write shorebird.yaml (strict RSA verification + local mock server)
        (app_dir / "shorebird.yaml").write_text(
            f"app_id: {app_id}\n"
            f"base_url: {mock.base_url}\n"
            f"auto_update: false\n"
            f"patch_public_key: {pub_b64}\n"
            f"patch_verification: strict\n",
            encoding="utf-8",
            newline="\n",
        )

        # Write minimal pubspec.yaml (zero external pub.dev dependencies)
        (app_dir / "pubspec.yaml").write_text(
            "name: lb_engine_test\n"
            "description: Loongbird pure-engine hot-update verification app.\n"
            "publish_to: 'none'\n"
            "version: 1.0.0+1\n"
            "environment:\n"
            "  sdk: '>=3.10.0 <4.0.0'\n"
            "dependencies:\n"
            "  flutter:\n"
            "    sdk: flutter\n"
            "flutter:\n"
            "  uses-material-design: false\n"
            "  assets:\n"
            "    - shorebird.yaml\n",
            encoding="utf-8",
            newline="\n",
        )

        (app_dir / "lib" / "main.dart").write_text(MAIN_DART, encoding="utf-8", newline="\n")
        (app_dir / "lib" / "patch_workload.dart").write_text(WORKLOAD_BASE, encoding="utf-8", newline="\n")

        run_cmd([*flutter_cmd, "pub", "get"], cwd=app_dir, env=build_env, timeout=300)

        # Build BASE Release
        run_cmd(
            [*flutter_cmd, "build", platform, "--release"],
            cwd=app_dir,
            env=build_env,
            timeout=600,
        )

        # Confirm our freshly built desktop release engine zip was fetched from the local proxy!
        if platform == "linux":
            rel_engine_key = f"flutter_infra_release/flutter/{engine_rev}/linux-x64-release/linux-x64-flutter-gtk.zip"
            bundle_src = app_dir / "build" / "linux" / "x64" / "release" / "bundle"
        else:
            rel_engine_key = f"flutter_infra_release/flutter/{engine_rev}/windows-x64-release/windows-x64-flutter.zip"
            bundle_src = app_dir / "build" / "windows" / "x64" / "runner" / "Release"

        if proxy.local_hits.get(rel_engine_key, 0) < 1:
            raise RuntimeError(
                f"Flutter build did NOT fetch local release engine {rel_engine_key} from proxy! "
                f"Local hits: {proxy.local_hits}"
            )

        release_bundle = work / "release_bundle"
        shutil.copytree(bundle_src, release_bundle)
        release_exe, base_libapp = find_release_app_exe(release_bundle, platform)
        _safe_print(f"[stage4] Base release bundle saved: {release_exe} (base AOT={base_libapp.stat().st_size} B)\n")

        # Build PATCH 1 Release
        (app_dir / "lib" / "patch_workload.dart").write_text(WORKLOAD_PATCH1, encoding="utf-8", newline="\n")
        run_cmd(
            [*flutter_cmd, "build", platform, "--release"],
            cwd=app_dir,
            env=build_env,
            timeout=600,
        )
        _, patch_libapp = find_release_app_exe(bundle_src, platform)
        patch_hash = sha256_file(patch_libapp)
        patch_sig = sign_sha256_hex(priv_pem, patch_hash)

        # Generate binary diff with our Rust `patch` tool
        patch_diff = work / "patch_1.bin"
        run_cmd([str(patch_bin), str(base_libapp), str(patch_libapp), str(patch_diff)])
        patch_bytes = patch_diff.read_bytes()
        _safe_print(
            f"[stage4] Patch 1 diff generated: {len(patch_bytes)} B "
            f"(patch AOT={patch_libapp.stat().st_size} B, sha256={patch_hash[:16]}...)\n"
        )
        mock.set_patch(patch_bytes, patch_hash, patch_sig)

        # Prepare isolated runtime environment for the 3-run desktop hot-update check
        run_env: dict[str, str] = {}
        if platform == "linux":
            isolated_home = work / "isolated_home"
            isolated_home.mkdir(parents=True, exist_ok=True)
            run_env["HOME"] = str(isolated_home)

        # Run 1: Boot BASE -> download Patch 1 -> verify RSA-2048 signature -> inflate -> promote
        out1 = run_desktop_app(
            release_exe,
            platform,
            "Run 1 (BASE + Download + RSA Verify + Inflate)",
            work / "run1.log",
            {**run_env, "LOONGBIRD_VERIFY_DO_UPDATE": "1"},
        )
        for token in [
            "marker=BASE",
            "available=true",
            "patch=0",
            "passed=6/6",
            "r1=PASS(10100)",
            "CHECK_BEFORE downloadable=true",
            "UPDATE_RESULT status=1",
            "UPDATE_DONE nextPatch=1",
        ]:
            if token not in out1:
                raise RuntimeError(f"Run 1 missing expected token '{token}':\n{out1}")
        # Signing must actually be enabled in the built artifact:
        # patch_public_key only reaches the bundled shorebird.yaml via
        # SHOREBIRD_PUBLIC_KEY (flutter_tools drops the yaml key). Whether the
        # signature is really enforced is proven by Run 4 below, which offers a
        # patch whose signature does not match and requires a refusal.
        if "patch_public_key: None" in out1:
            raise RuntimeError(f"Run 1 built without patch_public_key:\n{out1}")
        if "FATAL err=" in out1:
            raise RuntimeError(f"Run 1 reported error:\n{out1}")

        # Run 2: Cold boot into Patch 1 (strict boot-time RSA re-verification)
        out2 = run_desktop_app(
            release_exe,
            platform,
            "Run 2 (Cold Boot into Patch 1 — Strict Boot RSA Verify)",
            work / "run2.log",
            {**run_env, "LOONGBIRD_VERIFY_DO_UPDATE": "0"},
        )
        for token in [
            "marker=PATCH1",
            "available=true",
            "patch=1",
            "passed=6/6",
            "r1=PASS(15150)",
            "CHECK_BEFORE downloadable=false",
        ]:
            if token not in out2:
                raise RuntimeError(f"Run 2 missing expected token '{token}':\n{out2}")
        if "FATAL err=" in out2:
            raise RuntimeError(f"Run 2 reported fatal error:\n{out2}")

        # Run 3: Stable re-boot — verify Patch 1 stays active with zero rollback
        out3 = run_desktop_app(
            release_exe,
            platform,
            "Run 3 (Stable Re-Boot — Zero Rollback)",
            work / "run3.log",
            {**run_env, "LOONGBIRD_VERIFY_DO_UPDATE": "0"},
        )
        for token in [
            "marker=PATCH1",
            "available=true",
            "patch=1",
            "passed=6/6",
            "r1=PASS(15150)",
        ]:
            if token not in out3:
                raise RuntimeError(f"Run 3 missing expected token '{token}':\n{out3}")
        if "FATAL err=" in out3:
            raise RuntimeError(f"Run 3 reported fatal error:\n{out3}")

        # Run 4: a patch whose RSA signature does not match must be refused.
        # This is the behavioural proof that signature verification is really
        # enforced (a log-string assertion depends on log levels/messages).
        downloads_before_reject = mock.download_count
        bad_signature = sign_sha256_hex(priv_pem, "0" * 64)
        mock.set_patch(patch_bytes, patch_hash, bad_signature, number=2)
        out4 = run_desktop_app(
            release_exe,
            platform,
            "Run 4 (Patch 2 With Wrong Signature Must Be Refused)",
            work / "run4.log",
            {**run_env, "LOONGBIRD_VERIFY_DO_UPDATE": "1"},
        )
        for token in ["marker=PATCH1", "available=true", "patch=1", "passed=6/6"]:
            if token not in out4:
                raise RuntimeError(f"Run 4 missing expected token '{token}':\n{out4}")
        if "UPDATE_RESULT status=1" in out4:
            raise RuntimeError(
                f"Run 4 INSTALLED a patch with an invalid RSA signature:\n{out4}"
            )
        if "UPDATE_DONE nextPatch=0" not in out4:
            raise RuntimeError(
                f"Run 4 left a bad-signature patch queued for next boot:\n{out4}"
            )
        downloads_after_reject = mock.download_count
        _safe_print(
            f"[stage4] Run 4 rejected the bad-signature patch "
            f"(downloads_before={downloads_before_reject}, "
            f"downloads_after={downloads_after_reject})\n"
        )

        # Run 5: the refusal must not disturb the running Patch 1.
        out5 = run_desktop_app(
            release_exe,
            platform,
            "Run 5 (Still Patch 1 After Refused Update)",
            work / "run5.log",
            {**run_env, "LOONGBIRD_VERIFY_DO_UPDATE": "0"},
        )
        for token in ["marker=PATCH1", "available=true", "patch=1", "passed=6/6"]:
            if token not in out5:
                raise RuntimeError(f"Run 5 missing expected token '{token}':\n{out5}")
        if "FATAL err=" in out5:
            raise RuntimeError(f"Run 5 reported fatal error:\n{out5}")

        # Verify Mock Updater Server observed the full protocol handshake
        if len(mock.check_requests) < 2:
            raise RuntimeError(f"Expected >= 2 patch check requests, got {len(mock.check_requests)}")
        if mock.download_count < 1:
            raise RuntimeError("Mock updater server never served /patches/1.bin")
        first_req = mock.check_requests[0]
        if first_req.get("app_id") != app_id or first_req.get("platform") != platform:
            raise RuntimeError(f"Unexpected patch check payload: {first_req}")
        if first_req.get("release_version") != "1.0.0+1":
            raise RuntimeError(f"Unexpected release_version: {first_req}")

        _safe_print(
            f"[stage4] PASS: 3-run {platform} desktop RSA-2048 strict hot-update verified! "
            f"(checks={len(mock.check_requests)}, downloads={mock.download_count}, "
            f"events={len(mock.events)}, local_proxy_hits={proxy.local_hits})\n"
        )
    finally:
        proxy.stop()
        mock.stop()
        if win_cache_dir and win_cache_dir.exists():
            shutil.rmtree(win_cache_dir, ignore_errors=True)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Pure-engine functional verification for Loongbird Linux & Windows artifacts."
    )
    parser.add_argument("--platform", required=True, choices=["linux", "windows"])
    parser.add_argument("--artifacts-dir", required=True)
    parser.add_argument(
        "--flutter-root",
        default=str(Path(__file__).resolve().parents[3]),
        help="Path to loongbird_flutter checkout root.",
    )
    parser.add_argument("--engine-rev", default="")
    parser.add_argument("--upstream-engine-rev", default=UPSTREAM_FLUTTER_3449_REV)
    parser.add_argument("--upstream-storage-url", default=UPSTREAM_STORAGE_URL)
    parser.add_argument("--workdir", default="")
    args = parser.parse_args(argv)

    flutter_root = Path(args.flutter_root).resolve()
    artifacts_dir = Path(args.artifacts_dir).resolve()
    engine_rev = args.engine_rev.strip() or (
        flutter_root / "bin" / "internal" / "engine.version"
    ).read_text(encoding="utf-8").strip()

    if args.workdir:
        work = Path(args.workdir).resolve()
        work.mkdir(parents=True, exist_ok=True)
        cleanup_work = False
    elif args.platform == "windows":
        work = Path(f"C:\\lb_ev_{int(time.time())}")
        work.mkdir(parents=True, exist_ok=True)
        cleanup_work = True
    else:
        work = Path(tempfile.mkdtemp(prefix="lb_engine_verify_"))
        cleanup_work = True

    _safe_print(
        f"[verify] platform={args.platform} engine_rev={engine_rev}\n"
        f"[verify] flutter_root={flutter_root}\n"
        f"[verify] artifacts_dir={artifacts_dir}\n"
        f"[verify] workdir={work}\n"
    )

    try:
        sdk_dir = verify_stage1_dart_sdk(artifacts_dir, engine_rev, args.platform, work)
        verify_stage2_tools_and_android_gen_snapshot(
            artifacts_dir, engine_rev, args.platform, work, sdk_dir
        )
        patch_bin = verify_stage3_patch_cli(artifacts_dir, engine_rev, args.platform, work)
        verify_stage4_desktop_hot_update(
            flutter_root=flutter_root,
            artifacts_dir=artifacts_dir,
            engine_rev=engine_rev,
            upstream_rev=args.upstream_engine_rev,
            upstream_storage_url=args.upstream_storage_url,
            platform=args.platform,
            work=work,
            patch_bin=patch_bin,
        )
        _safe_print(
            f"\n[verify] ALL 4 STAGES PASSED for {args.platform} (engine={engine_rev})!\n"
        )
        return 0
    finally:
        if cleanup_work and work.exists():
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
