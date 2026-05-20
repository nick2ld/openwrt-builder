# Changelog

All notable changes to OpenWrt Custom Local Builder are tracked here.

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
