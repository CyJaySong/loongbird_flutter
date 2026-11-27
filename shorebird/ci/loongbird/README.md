# Loongbird Desktop & Cross-Host CI (Phase D2)

Automated GitHub Actions build, gateway upload, and zero-local-engine desktop E2E verification for **Linux x64** and **Windows x64** (complementing macOS-built iOS, Android, and macOS artifacts from Phase D1).

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

## Required GitHub Repository Secrets (`cyjaysong/loongbird_flutter`)

1. `LOONGBIRD_DART_SDK_TOKEN`: GitHub fine-grained PAT with `Contents: Read` on `cyjaysong/loongbird_dart_sdk` (required while `loongbird_dart_sdk` is private; omit if public).
2. `LOONGBIRD_API_TOKEN`: Loongbird Engine Artifact Gateway Bearer token (`sb_api_...`), required when `upload_to_gateway=true` or `run_e2e=true`.
3. `LOONGBIRD_CLI_GIT_URL`: Git URL of the `loongbird` CLI repository (e.g. `https://gitcode.com/CyJay/loongbird.git` or a GitHub mirror), required for `e2e-linux` and `e2e-windows`.
4. `LOONGBIRD_CLI_GIT_TOKEN`: Access token (or `username:token`) if `LOONGBIRD_CLI_GIT_URL` is private.

## Triggering

> **Important (GitHub default branch):** GitHub Actions only exposes `workflow_dispatch` workflows that exist on the repository's **default branch**. In `cyjaysong/loongbird_flutter` → **Settings → General → Default branch**, switch the default branch from `master` to `loongbird-3.44.9` before triggering.

In GitHub → `Actions` → **Loongbird Desktop & Cross-Host CI (Linux & Windows)** → **Run workflow**:
- `platforms`: `all` | `linux` | `windows`
- `upload_to_gateway`: `true`
- `allow_413_fallback`: `true` (if an edge CDN caps single PUT bodies at 100 MiB, `dart-sdk-*.zip` is still saved in GitHub Actions Artifacts)
- `run_e2e`: `true`

## Local Fallback Sync (if any large zip hit CDN HTTP 413)

Download the workflow artifact zip locally, extract it to `<dir>`, and stream directly to your local gateway:

```bash
python3 shorebird/ci/loongbird/upload_artifacts.py \
  --upload-dir <extracted_dir> \
  --gateway-url http://127.0.0.1:8080 \
  --token "$SHOREBIRD_TOKEN"
```
