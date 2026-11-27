# Loongbird Desktop & Cross-Host Engine CI (Phase D2)

Automated GitHub Actions build and **pure-engine functional verification** for **Linux x64** and **Windows x64** (complementing macOS-built iOS, Android, and macOS artifacts from Phase D1).

Engine verification runs completely standalone inside `loongbird_flutter` — **zero dependency on `loongbird` CLI or external self-hosted gateways**.

## Artifacts Produced per Engine Revision (`bin/internal/engine.version`)

### Linux x64 (`ubuntu-22.04`)
| Shard | Artifacts |
| --- | --- |
| `linux-host-release` | `flutter_infra_release/flutter/$engine/dart-sdk-linux-x64.zip`, `flutter_infra_release/flutter/$engine/linux-x64-release/linux-x64-flutter-gtk.zip`, `shorebird/$engine/patch-linux-x64.zip` |
| `linux-host-debug` | `flutter_infra_release/flutter/$engine/linux-x64/artifacts.zip` |
| `linux-android-gen-snapshot` (`arm64`, `arm`, `x64`) | `flutter_infra_release/flutter/$engine/android-{arm64,arm,x64}-release/linux-x64.zip` |

### Windows x64 (`windows-2022`)
| Shard | Artifacts |
| --- | --- |
| `windows-host-release` | `flutter_infra_release/flutter/$engine/dart-sdk-windows-x64.zip`, `flutter_infra_release/flutter/$engine/windows-x64-release/windows-x64-flutter.zip`, `shorebird/$engine/patch-windows-x64.zip` |
| `windows-host-debug` | `flutter_infra_release/flutter/$engine/windows-x64/artifacts.zip` |
| `windows-android-gen-snapshot` (`arm64`, `arm`, `x64`) | `flutter_infra_release/flutter/$engine/android-{arm64,arm,x64}-release/windows-x64.zip` |

## Pure-Engine Functional Verification (`verify_engine.py`)

Both `verify-linux` and `verify-windows` execute 4 stages against the staged GHA artifacts:

1. **Dart SDK**: Zip integrity, null SDK hash (`0000000000`), `devtools` & `dartdoc` presence, `dart --version`, `dartvm` JIT, `dart compile exe` native AOT, and `dart compile aot-snapshot` + `dartaotruntime`.
2. **Host Debug Tools & Android 3-ABI `gen_snapshot`**: `flutter_tester` / `impellerc` / `icudtl.dat`, `frontend_server_aot.dart.snapshot` kernel compilation, and real Android `arm64`/`arm`/`x64` AOT ELF generation with ELF `e_machine` validation (`EM_AARCH64=183`, `EM_ARM=40`, `EM_X86_64=62`).
3. **Rust `patch` Binary Diff CLI**: Synthetic binary diff generation.
4. **Full Flutter Desktop Release + 3-Run Live RSA-2048 Strict Hot-Update**:
   - Local in-process Artifact Proxy (`127.0.0.1`) serves our built zips and 302-redirects untouched upstream assets directly to `storage.googleapis.com`.
   - Local in-process Mock Updater Server (`127.0.0.1`) implements `/api/v1/patches/check`, `/patches/1.bin`, and `/api/v1/patches/events`.
   - Compiles BASE + PATCH1 Release desktop apps, generates a real binary diff with our Rust `patch`, signs Patch 1 SHA-256 with RSA-2048 (`patch_verification: strict`), and runs 3 consecutive launches verifying download → RSA verify → inflate → cold boot into Patch 1 (6/6 stress tests) → stable re-boot with zero rollback.

## Required / Optional GitHub Repository Secrets (`cyjaysong/loongbird_flutter`)

1. `LOONGBIRD_DART_SDK_TOKEN`: GitHub fine-grained PAT with `Contents: Read` on `cyjaysong/loongbird_dart_sdk` (required while `loongbird_dart_sdk` is private; omit if public).
2. `LOONGBIRD_API_TOKEN` *(optional)*: Loongbird Engine Artifact Gateway Bearer token (`sb_api_...`), only needed when `upload_to_gateway=true` or `skip_existing_builds=true`.

## Triggering

In GitHub → `Actions` → **Loongbird Desktop & Cross-Host CI (Linux & Windows)** → **Run workflow**:
- `platforms`: `all` | `linux` | `windows`
- `verify_only`: `true` to skip all build shards and run pure-engine verification directly on the latest GHA artifacts; `false` to build + verify
- `run_verify`: `true`
- `upload_to_gateway`: `false` (or `true` to also push artifacts to gateway)
