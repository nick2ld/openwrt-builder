# Changelog

All notable changes to OpenWrt Custom Local Builder are tracked here.

## [0.2.7] - 2026-05-21

### Fixed
- ImageBuilder now gets a real `tmp` directory through `TMPDIR`, avoiding temporary-directory warnings.
- ImageBuilder now uses the system OpenSSL config when available, reducing `openssl.cnf` warnings.
- Local APK package indexes are refreshed before image builds when external APKs are present.

## [0.2.6] - 2026-05-21

### Fixed
- Router search now normalizes target/subtarget values and reads architecture from both target and profile metadata.
- Router saving now blocks incomplete entries without Target, Subtarget, Profile, or Arch.

## [0.2.5] - 2026-05-21

### Fixed
- Stopping a build now cancels every active job for the same router instead of leaving a duplicate build running.
- Build cancellation is checked between ImageBuilder/APK preparation phases, before `make image` is started.

## [0.2.4] - 2026-05-21

### Fixed
- ImageBuilder downloads and extraction are locked per release/target to avoid `.part` file races.
- Starting a real build now clears stale active jobs for the same router while keeping the current job.
- Stale active jobs are marked as failed when the service starts, so old queued/downloading rows do not block new builds.

## [0.2.3] - 2026-05-21

### Fixed
- Manual router builds no longer create a second active job when the same router is already building.
- The build log modal now shows that an existing build was reused instead of opening a duplicate queued job.

## [0.2.2] - 2026-05-21

### Fixed
- Self-update status no longer reads stale success lines from previous update attempts.
- The self-update log is reset at the start of a new web UI update run.

## [0.2.1] - 2026-05-21

### Changed
- Failed jobs now show a red cross icon and cancelled jobs show a neutral dash icon in the job list.

## [0.2.0] - 2026-05-21

### Added
- Modern web UI with router list, APK source list, modals, progress bars, live logs, and RU/EN localization.
- OpenWrt Firmware Selector style router search with automatic profile, target, subtarget, arch, and default package detection.
- Per-router package lists and per-source external APK settings.
- External APK discovery for direct links, repository indexes, and GitHub Releases.
- ASU-compatible endpoints for LuCI Attended Sysupgrade.
- Router ASU request/response log in the UI.
- Manual firmware deletion from the latest firmware modal.
- Self-update UI with progress modal and update log.
- GitHub Release workflow that publishes a zip archive for tagged versions.

### Changed
- Application update checks now use semantic release versions instead of displaying only commit-to-commit changes.
- Failed and cancelled jobs are retained in the UI instead of disappearing immediately.
- Job state writes are protected from concurrent updates.
- ASU build requests are deduplicated by request hash and active router build.

### Fixed
- Oversized firmware images are reported as failed builds with a clear error instead of being shown as successful.
- Repeated oversized auto-build loops are blocked until the package/source configuration changes.
- Cached firmware is returned to ASU clients when it matches the requested router and packages.
- Self-update systemd helper status tracking.
