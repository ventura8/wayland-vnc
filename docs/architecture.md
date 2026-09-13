# Architecture

The Python CLI probes session capabilities without changing services. GNOME needs
both Mutter RemoteDesktop and ScreenCast bus owners; KWin needs both portal
interfaces; WayVNC needs capture plus virtual keyboard and virtual pointer protocols.
An environment-variable desktop name is only a diagnostic hint.

Backend candidates are not qualification claims. Installation, service activation and
rollback are implemented and validated: the staging installer writes a checksummed
manifest with backups and an exact rollback, the packages enable the systemd user unit
and start it for every logged-in user, and removal stops it again, all exercised by the
package smokes and the hardware run. The CLI's own `start`/`stop` deliberately change
nothing; they name the session-manager command instead. Nothing here ever operates on a
distribution service just because its name resembles ours.

The intended private GNOME package contains a VNC-only daemon, project-scoped schema
loading and a private LibVNC library. Workarounds are separate from correctness
patches. Neither `/usr/bin/grdctl` nor the system library may be overwritten.

The qualification validator requires all fourteen desktop/viewer combinations,
exact environment versions, matching commit, complete scenarios, first-frame and
reconnection limits, and hashes for viewer captures, input results and runner logs.
It rejects skipped tests and missing artifacts. Hashes establish integrity, not
truth: only a trusted isolated runner may supply release records. Unit-test dummy
records are never published as real evidence.

Only synthetic screens belong in artifacts. Broad desktop support is an ongoing
release target; `qualification/records.json` intentionally starts empty.
