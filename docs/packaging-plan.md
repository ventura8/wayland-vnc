# Packaging, CI, release, and unblock plan

Living checklist for the "full installer + CI/PPA/GitHub release + agent files"
initiative. Keep it in sync as items land; do not delete completed rows (mark them).

## 1. Agent files & skills — DONE

- [x] `AGENTS.md` expanded (overview, version SSOT, safety rules, no-suppressions,
      lint/test/pipeline/packaging/release/qualification sections, skill tree).
- [x] `.agents/skills/code-linter`, `test-runner`, `pipeline-runner`,
      `fixture-qualifier`, `native-patch-verifier`, `release` (wayland-vnc specific).
- [x] `.agents/skills/resolve-pr-comments`, `review-with-coderabbit` (generic, reused).

## 2. Version single source of truth — DONE

- [x] Root `VERSION` = `1.0.0`; `scripts/sync-version.py` (+ `--check`) syncs
      `pyproject.toml [project] version`; wired into `build-and-test.sh --full`.

## 3. Full multi-format installer (real product, not just diagnostics)

The installed product is a real Wayland VNC server (WayVNC) **plus** the diagnostic
tool. It installs, enables and starts a systemd **user** service
(this machine only by default; the local network is an explicit switch),
ships provisioning (`set-password`, `provision`), `serve` (systemd ExecStart), and the
`doctor`/`status` diagnostics. Shared payload: `packaging/stage-payload.sh`.

- [x] CLI serving runtime (`runtime.py`: provision/set-password/serve, local-network default,
      600 perms, fail-closed) + CLI subcommands + unit tests (100% cli, 98% runtime).
- [x] Hardened `packaging/systemd/wayland-vnc.service` (user unit, IPAddressDeny=any).
- [x] Debian packaging (`debian/`): control (Depends wayvnc, openssl; payload scoped to
      runtime modules, no Pillow),
      rules, changelog, copyright, source/format, postinst (enable --global),
      prerm (disable), postrm (remove module tree). Launcher sets
      `sys.dont_write_bytecode`.
- [x] RPM spec (`packaging/rpm/wayland-vnc.spec`, version from VERSION, %post/%postun).
- [x] Arch `PKGBUILD` (`packaging/arch/PKGBUILD`).
- [x] AppImage (`packaging/appimage/`) — AppRun + desktop + icon + build branch.
- [x] Flatpak (`packaging/flatpak/`) — manifest + build branch.
- [x] Snap (`packaging/snap/snapcraft.yaml`) — manifest.
- [x] `scripts/build_release_package.sh KIND` + `prepare_release_package_host_deps.sh`
      (deb+rpm verified locally).

## 4. Launcher TUI — DONE

- [x] `src/wayland_vnc/tui.py` — install / diagnostics / uninstall menu, state-aware
      (uninstall only when provisioned), injectable Screen for tests, curses adapter.
- [x] `wayland-vnc-setup` console entry (pyproject + stage-payload launcher).
- [x] Unit tests for the menu state machine with a scripted fake screen (tui 99%).

## 5. Real Docker install/uninstall smoke — ALL scenarios, ALL platforms

Shared `scripts/package_smoke_scenarios.sh` (happy: diagnostic, set-password 600,
provision loopback/600, serve execs fake wayvnc; bad: privileged port, mismatched
password, X11 refused, serve without password). Per-format drivers build → install →
scenarios → reinstall → truncated-package rejection → remove/purge clean → idempotent.

- [x] `scripts/run_deb_package_smoke.sh` (Ubuntu 26.04 + Debian trixie) — PASSED 26.04.
- [x] `scripts/run_rpm_package_smoke.sh` (Fedora 44, AlmaLinux 10, openSUSE TW) —
      package-mechanics via rpm --nodeps.
- [x] `scripts/run_arch_package_smoke.sh` (Arch, non-root makepkg --nodeps).
- [x] Portable smoke (`scripts/run_portable_package_smoke.sh`, ubuntu:26.04 containers):
      **AppImage** builds and runs the FULL happy+bad scenario suite against the
      extracted AppRun, rejects a truncated image, and asserts zero system footprint;
      **Snap** builds the classic-confinement squashfs payload + `meta/snap.yaml`,
      unsquashes it and runs the FULL suite, rejects a truncated snap (a real
      `snap install` needs snapd/systemd on a host); **Flatpak** validates the manifest
      and runs the diagnostic against the exact `/app` payload its build-commands stage
      (a real flatpak-builder/run cycle needs the fdo 24.08 runtime + bubblewrap userns
      on a host). Wired into `build-and-test.sh --packages` and the CI `portable-smoke`
      matrix. Fixed a real defect this surfaced: the console launchers hardcoded an
      absolute prefix; they now resolve the module tree relative to their own path so
      the same payload works from /usr, $SNAP/usr, /app, or an AppDir.

## 6. CI + local pipeline

- [x] Local `./scripts/build-and-test.sh --full` = version-sync, lint (incl. packaging),
      native ASan/UBSan, systemd units, unit+coverage (≥90% per file), fixture smokes,
      deb smoke. `--packages` runs deb+rpm+arch smoke.
- [x] `.github/workflows/ci.yml` — checks job + fixture-smoke matrix + package-smoke
      matrix (deb/rpm/arch), actions pinned by SHA, `.github/actionlint.yaml` label.
- [x] `.github/dependabot.yml` — github-actions + pip (weekly).

## 7. GitHub + PPA release automation

- [x] **The source upload is verified without credentials.**
      `scripts/run_ppa_source_smoke.sh` builds the very source package the release
      workflow signs, in a clean Ubuntu 26.04 container, with the same generated
      changelog: it checks that the changelog parses, that
      `<version>+1ppa<rev>~<series>1` sorts above the plain version and below the next
      patch release, that the upload is source-only with a `.dsc` and a native
      tarball, and that lintian reports no errors. In the `--full` and `--packages`
      gates.
- [x] **The Launchpad side is the maintainer's to do once**, and only the maintainer
      can: the account and Code of Conduct, the `wayland-vnc` PPA, an OpenPGP key
      Launchpad has confirmed, and the `GPG_PRIVATE_KEY` and `GPG_PASSPHRASE` secrets
      in the `ppa-release` environment. Done 2026-09-17 (verified from the public
      Launchpad pages and `gh secret list`), and the signing path was exercised once
      locally with `debsign` and `dput --simulate`. Step by step in
      [ppa-setup.md](ppa-setup.md). The same day the maintainer decided that
      `upload-to-ppa` runs on every tag rather than waiting for qualification; the
      generated changelog states the gate's verdict, and the GitHub release keeps its
      prerelease flag until the evidence exists.

- [x] `.github/workflows/release.yml` on `vX.Y.Z` tag: validate VERSION==tag==pyproject,
      build+sign Debian source, `dput ppa:ventura8/wayland-vnc` (resolute),
      `PPA_UPLOAD_REVISION`; build-packages matrix (deb/rpm/arch/appimage/flatpak/snap);
      github-release job with `SHA256SUMS` + generated notes. GPG via repo secrets.
- [x] `docs/releases/v1.0.0.md` + `v1.0.0_github_description.md`.

## 8. Unblock previously-blocked targets

User asked to unblock these using laptop control. Status:

- [x] **Hyprland — UNBLOCKED (partial evidence).** A disposable KVM guest
      (`scripts/kvm/build-hyprland-guest.sh`) boots Hyprland 0.53.3 + aquamarine on a
      virtio-gpu KMS device (Mesa llvmpipe EGL/GBM), runs the native GTK scene and
      WayVNC 0.9.1, and the **actual RealVNC Viewer 7.15.1** connects over RA2-256/
      AES-256 and renders the ordered RGBW scene with live changing frames. The host
      GNOME session and GPU are never touched. Root cause of the earlier "empty
      desktop" capture: the virtio-gpu guest enumerates two connectors, and the
      fixture must be on the captured one -- the compositor configs now disable
      `Virtual-2` from the start and WayVNC is told to capture `Virtual-1` (the
      Hyprland guest has since moved onto the generic guest template,
      `scripts/kvm/build-guest.sh hyprland`). Evidence:
      `artifacts/desktop-viewer/hyprland/kvm-*/partial-evidence.json` (first-frame,
      changing-frames). Input/portal/resize scenarios are not run in the VM harness,
      so this is partial evidence, not a release qualification.
- [x] **suspend-resume — UNBLOCKED (partial evidence).** Real ACPI S3 in the same KVM
      guest via the QEMU guest agent: QMP `query-status` transitions running ->
      suspended (running:false) after `guest-suspend-ram`, then running again after
      `system_wakeup`. The actual RealVNC Viewer captures a valid RGBW scene before
      suspend and, after wake, reconnects/authenticates and captures a valid RGBW
      scene with live-advancing frames. Evidence:
      `artifacts/desktop-viewer/hyprland/suspend-resume-*/partial-evidence.json`.
- [x] **Android — UNBLOCKED (partial evidence).** The actual RealVNC Android app
      (4.9.4.60176) connects to WayVNC in the KVM Hyprland guest over an `adb reverse`
      loopback tunnel (emulator 5900 -> host 5910, never exposed off-host) and renders
      the live scene: R/G/B arrive pixel-exact and two captures 1.5s apart differ, so
      frames are genuinely live. Evidence:
      `artifacts/desktop-viewer/android/realvnc-*/partial-evidence.json`
      (app-launch, direct-connection, server-identity-check, authentication,
      first-frame, colour-fidelity, changing-frames).
      Provenance: RealVNC publishes no first-party APK (verified 2026-09-14: their
      Android page links only to Google Play), so the agent never downloaded one. The
      user supplied the APK as a local file; before use its signature was verified with
      `apksigner` (APK Signature Scheme v2 verifies; signer
      `CN=RealVNC Ltd, O=RealVNC Ltd, L=Cambridge, C=UK`, cert SHA-256
      `66d81472a2cad46121b6db13870a761425a833c93ca128c8b46a64d80307b7d9`). No Google or
      RealVNC account was used or created. (Mirrors were forbidden at the time; on
      2026-09-17 the maintainer permitted them, with RealVNC's signature mandatory --
      see the Android entry in section 17 and [android.md](android.md).)
      The viewer's fixed zoom keeps the white band off-viewport, so the full four-band
      `verify_scene` assertion stays desktop-only; `scripts/android-stage.sh` boots the
      isolated AVD and wires the tunnel.

## 9. Native GTK4 settings app — DONE

`wayland-vnc-settings`: a GTK4/libadwaita window beside the CLI, TUI and installer.
libadwaita means it adopts the host desktop's theme, accent and light/dark preference,
so it reads as a first-party app. It shows live status and REAL options only.

- [x] `src/wayland_vnc/settings.py` — GTK-free logic layer: `gather()` builds `Status`
      from the real `doctor` verdict, the credential file (presence + mode), the
      provisioned `wayvnc.conf` (address/port/auth/loopback) and `systemctl --user`
      unit state. Every boundary injectable; pylint 10/10, 98% covered.
- [x] `src/wayland_vnc/settings_app.py` — the libadwaita window (status group, option
      rows, service switches). GTK bound through `importlib` so version selection
      happens before the typelibs load, with no lint suppression anywhere. 95% covered
      by headless tests against a private Xvfb server.
- [x] **Light and dark mode, following the OS.** The app never pins a colour scheme:
      it leaves `Adw.StyleManager` on `ColorScheme.DEFAULT`, so libadwaita tracks the
      desktop's `color-scheme` preference (and the portal setting under Flatpak) and
      restyles live when the user switches. It ships no `CssProvider` and no hardcoded
      colours, so the desktop's palette, accent and high-contrast settings always win.
      Regression-tested three ways: the scheme is still DEFAULT after the window is
      built (nothing pins it), forcing dark/light flips what libadwaita resolves, and
      the source is asserted free of `set_color_scheme`, `CssProvider`, hex and rgb()
      literals. Verified on the maintainer's GNOME laptop (`prefer-dark`/Yaru-dark →
      libadwaita resolves dark) and rendered in both modes.
- [x] **Options are real, never placebo.** `Actions.set_credential` and
      `apply_network` go through `runtime.set_password` / `runtime.provision`, so they
      inherit mode-600 writes and the CLI's own validation (privileged ports and weak
      passwords are refused). The service switches call `systemctl --user`
      enable/disable/start/stop and raise with systemd's stderr on failure.
- [x] **Unbacked controls are disabled with the reason**, never dead toggles:
      `Actions.options()` gates each row and `_service_reason()` explains exactly what
      is missing (no unit installed / no credential yet / not a Wayland session).
- [x] Packaging: `wayland-vnc-settings` console entry, a staged launcher, and a
      `.desktop` entry. GTK is a Recommends (not Depends) so headless server installs
      stay lean, and the app raises a clear install hint when GTK is absent.
- [x] **Entry dialogs** (`src/wayland_vnc/settings_dialogs.py`): native `Adw.Dialog`s
      for the credential (username, password, confirm -- `Adw.PasswordEntryRow` with
      reveal), the bind address/port (`Adw.EntryRow` + `Adw.SpinRow` whose range
      already excludes privileged ports, so 80/443 cannot even be selected), and
      diagnostics (the raw versioned JSON). Each writes through `settings.Actions`, so
      it inherits the runtime's validation and mode-600 handling; errors are shown in
      the dialog, never swallowed, and nothing is written on a refused submit. The
      window re-renders from disk state after every commit. Each form exposes the
      `submit` handler its Save button runs, so tests and the e2e drive the exact same
      code path headlessly. 98% covered; the e2e exercises every dialog against the
      packaged app (which also caught the module missing from the payload). The
      settings app is now fully self-contained.
- [x] **Search in the diagnostics and language dialogs, the HIG way.** Diagnostics
      get a `Gtk.SearchBar` under the header with a toggle and type-to-search; the
      language chooser has its entry above the list. Both filter live, open the
      matching section, and show an `Adw.StatusPage` empty state rather than a blank
      list. Search is diacritics-tolerant in both directions: `settings_dialogs.fold`
      decomposes each side (NFKD), drops combining marks, maps the letters that do
      not decompose (ø, ł, đ, ı, æ, œ ...) and casefolds, so "romana" finds "Română",
      "strasse" finds "Straße", and an accented query finds plain text too. Unit
      tests cover the folding table and both dialogs; the language e2e searches
      "ROMANA" and requires exactly the Romanian row to survive.
- [x] **Icon and app-drawer presence, installed by the packages.**
      The icon is drawn in the host desktop's own idiom, matching Ubuntu's Yaru
      Settings icon whose palette and shadow were sampled from the installed theme
      (`/usr/share/icons/Yaru/256x256/apps/org.gnome.Settings.png`): light circular
      badge with Yaru's horizontal tonal band (upper `#d5d5d5`, lighter `#eaeaea`
      below), dark inner disc `#5d5d5d`, light glyph `#d8d8d8`, and a soft drop shadow
      (~18% black, 1px down, 1.5px blur). Rendered values were checked against the
      sampled originals and match exactly. A 16x16 monochrome symbolic variant ships
      alongside for small sizes. `stage-payload.sh` installs both into hicolor, the deb
      postinst/postrm and rpm %post/%postun refresh `update-desktop-database` and
      `gtk-update-icon-cache`, and the rpm `%files` lists launcher, entry and icons.
      Verified on the maintainer's GNOME laptop: the entry loads, `should_show()` is
      true, it is in `Gio.AppInfo.get_all()`, and the GTK icon theme resolves both
      names to the installed SVGs.
      Two real defects were found here by testing rather than assumed away:
      (1) a long leading XML comment pushed the `<svg` signature out of the window
      gdk-pixbuf sniffs, so the icons were well-formed XML yet silently unloadable --
      the comments now sit inside `<svg>`, and the e2e rasterises both icons to prove
      it; (2) an SVG icon needs a pixbuf loader, which Debian trixie and openSUSE lack
      by default, so the packages now recommend `librsvg2-common` / `librsvg2` /
      `librsvg` per format and the e2e containers install it.
      Note: GLib's `DesktopAppInfo` returns NULL unless the `Exec` binary is on PATH,
      so any drawer check must install the launcher too, not just the entry.
- [x] CI checks job installs `xvfb`, `python3-gi`, `gir1.2-gtk-4.0`, `gir1.2-adw-1`
      and builds its venv with `--system-site-packages`, so the GTK tests and pylint
      resolve `gi` there as they do locally.

## 10. Settings app end-to-end tests — DONE

`scripts/run_settings_app_e2e.sh` drives the PACKAGED app (staged payload, installed
launcher, real widget tree, real runtime, real files) rather than the source tree, and
runs on every supported platform. `scripts/settings_app_e2e_scenarios.py` holds the
scenarios; a fake `systemctl` on PATH captures service calls so the host is untouched.

- [x] Platform matrix: Ubuntu 26.04, Debian trixie, Fedora 44, AlmaLinux 10,
      openSUSE Tumbleweed, Arch — each installs that distro's own GTK4/libadwaita
      stack, plus a no-GTK container.
- [x] Happy: fresh host reports nothing configured; credential written mode 600;
      provisioning writes a loopback, authenticated, 0600 config; the real window
      builds and its rows appear in the live widget tree; toggling the live switches
      runs `systemctl enable/start/disable`; the installed launcher starts the window
      from the packaged tree with no traceback; icon, entry and drawer visibility.
- [x] Bad: weak password refused and nothing written; privileged port refused and no
      config written; options gated with the exact reason before setup; a failing
      systemctl surfaces systemd's stderr; a 0644 credential is flagged "too open";
      a non-loopback bind is called out; a non-Wayland session disables the service
      control; and with no GTK installed the app exits non-zero with an actionable
      install hint and no import traceback, while the CLI/service path still passes
      the full scenario suite.
- [x] Two real defects were found and fixed by these tests: the app crashed with a
      traceback on hosts without `systemctl` (now reports the unit absent), and the
      missing-GTK path leaked a raw `ModuleNotFoundError` (now a clean message).
- [ ] AlmaLinux 10 (and RHEL 10 rebuilds) ship neither Xvfb nor a GTK broadway
      backend, so no headless display exists there. The widget scenarios are reported
      as SKIPPED on that platform rather than silently passing; every
      display-independent scenario still runs. Revisit if EL10 gains a headless path.

## 11. GUI availability policy on every supported platform

Rule: on a supported platform the settings app must either have its dependencies
installed by our own package, or the product must offer a native-feeling alternative
there. It must never simply be missing or broken.

Measured availability (from the section 10 e2e matrix, which asserts the typelibs
import before running): **GTK4 and libadwaita are available on all six supported
platforms** — Ubuntu 26.04, Debian trixie, Fedora 44, AlmaLinux 10, openSUSE
Tumbleweed and Arch. No supported platform currently lacks GTK4.

- [x] The installer declares the GUI stack per format, so a desktop install pulls it
      automatically while a headless server install stays lean:
      deb `Recommends: python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1` (apt installs
      Recommends by default); rpm `Recommends: python3-gobject, gtk4, libadwaita`
      (dnf and zypper honour weak deps); Arch `optdepends` naming each with its reason,
      which is the Arch-native idiom.
- [x] Where the GUI genuinely cannot run, the product is still native and complete,
      not broken: `wayland-vnc-setup` is a curses TUI and `wayland-vnc` is a full CLI,
      both of which cover install, diagnostics, provisioning and uninstall. The GTK app
      exits non-zero with an actionable install hint (never an import traceback), and
      the e2e proves the CLI/service path passes its whole scenario suite with no GTK
      installed at all.
- [ ] If a future supported platform ships no GTK4, resolve it one of two ways before
      that platform is declared supported: (a) have our package install the needed
      dependencies there, or (b) add a backend that is native to that OS. Do not ship a
      platform where the settings app is simply absent.
- [ ] AlmaLinux 10 caveat: GTK4 is present, but EL10 provides no Xvfb and no broadway
      backend, so the GUI cannot be exercised headlessly in CI there. This limits
      testing, not the product: on a real EL10 desktop the app has a display and runs.

## 12. Installer must enable AND activate the service

Requirement: installing the package must leave the service enabled *and* running,
not merely enabled for some future login.

- [x] `systemctl --global enable wayland-vnc.service` enables the unit for every
      user, so it starts automatically at each next graphical login.
- [x] **Activate now for users already logged in.** The unit is a systemd *--user*
      unit, so root's maintainer script cannot start it directly. Following the
      approach used by the reference repo (asus-zenbook-linux-tools,
      `lib/install-components.sh:_enable_ydotool_user_unit`), the postinst enters each
      logged-in user's own session manager -- `runuser -u <user> -- env
      XDG_RUNTIME_DIR=/run/user/<uid> DBUS_SESSION_BUS_ADDRESS=unix:path=.../bus
      systemctl --user enable --now wayland-vnc.service` -- guarded on that runtime
      directory and bus socket actually existing. Implemented in
      `debian/wayland-vnc.postinst` and the rpm `%post`.
- [x] Also adopted from the reference: a `_systemd_is_live` guard that accepts
      running/degraded/starting/maintenance before touching systemd, so the same
      maintainer script is safe in containers and chroots, and every systemd call is
      best-effort so a package install never fails on it.
- [x] ~~**Deliberate exception:** a user with no stored credential is enabled but not
      started.~~ **Superseded on 2026-09-15 by section 14** and kept here only so the
      history reads straight: `runtime.serve` now generates a strong random credential
      on first start, so there is no no-credential case to except. The postinst and the
      rpm `%post` start the service for every logged-in user unconditionally, and the
      activation smoke requires BOTH users -- with and without a stored credential --
      to end `is-active=active`.
- [x] Verified on a real GNOME desktop (2026-09-15): after provisioning a credential,
      the activation path took the unit to `is-enabled: enabled` and
      `is-active: active`. This also surfaced a real defect: on GNOME, wayvnc exits
      immediately (`Virtual Pointer protocol not supported by compositor` -- Mutter
      offers no wlr virtual-pointer/screencopy, which is exactly what
      `wayland-vnc doctor` reports by naming `grd` rather than `wayvnc`), and with
      `Restart=on-failure` the unit restarted forever and filled the journal. The unit
      now carries `StartLimitIntervalSec=60` / `StartLimitBurst=3`, verified on the
      same host to stop after exactly 3 attempts and settle in `failed`.
- [x] **Verified under a real systemd** (`scripts/run_service_activation_smoke.sh`,
      wired into `build-and-test.sh --packages` and its own CI job): systemd runs as
      PID 1 in a privileged container, two lingering users get real user managers,
      the real `.deb` installs so the real postinst runs, and the outcome is asserted
      per user -- the user with a credential ends `is-enabled=enabled` and
      `is-active=active` with the unit having exec'd `wayvnc --config`, while the user
      without one is enabled but deliberately not started (fails closed). A stand-in
      `wayvnc` that sleeps lets the unit reach `active` without a compositor; the
      installer's activation is what is under test.
- [x] **Uninstall defect found by that smoke and fixed:** `prerm` only ran
      `--global disable`, which stops future logins but left the VNC server *running*
      for every logged-in user after the package was removed. `prerm` and the rpm
      `%preun` now `disable --now` inside each logged-in user's manager first, and the
      smoke asserts nothing is active after purge.

## 13. Adopted from the reference project, and agent-agnostic agent files

Reviewed `asus-zenbook-linux-tools` (code, CI and local pipeline) and took what
genuinely applies here rather than copying wholesale.

- [x] **User-service activation pattern** (section 12): its
      `lib/install-components.sh:_enable_ydotool_user_unit` enters the target user's
      own manager with `XDG_RUNTIME_DIR` and `DBUS_SESSION_BUS_ADDRESS` rather than
      using `--machine`, guarded on the runtime dir and bus socket existing. Adopted
      verbatim in spirit for `wayland-vnc.service`.
- [x] **`_systemd_is_live` guard** from its `debian/postinst`: treat
      running/degraded/starting/maintenance as live before touching systemd, and keep
      every systemd call best-effort so an install never fails on it. Adopted.
- [x] **Markdown is linted like code.** The reference lints Markdown at 140 columns;
      we had 43 Markdown files and no linter at all. Added `pymarkdownlnt` (pinned in
      `requirements-dev.txt`, so Dependabot tracks it) with `.pymarkdown.json`
      enabling front-matter parsing -- without it every `SKILL.md` YAML header is
      misread as a setext heading and cascades into false MD003/MD041/MD022. Wired
      into `build-and-test.sh --full` and the CI lint job; currently zero findings.
- [x] **yamllint now covers the packaging manifests.** The existing `.yamllint.yaml`
      was only applied to `.github`; `packaging/flatpak` and `packaging/snap` were
      unlinted. Both are included now (and `snapcraft.yaml` gained its `---` document
      start to match the flatpak manifest).
- [x] Already had, so nothing to adopt: `concurrency` with `cancel-in-progress`,
      least-privilege `permissions`, SHA-pinned actions, per-file coverage gate,
      Docker-based package smokes, `VERSION` single source of truth.
- [ ] Not adopted, deliberately: coverage sharding across matrix jobs (our suite runs
      in ~36s, so sharding would add CI complexity for no gain) and the
      `install.sh.sha256` self-checksum (we ship packages, not a curl-pipe installer).

Agent files are tool-neutral:

- [x] `AGENTS.md` is the canonical instruction set and contains no vendor-specific
      wording (audited: zero Claude/Anthropic references). It gained an
      "Agent compatibility" section stating this explicitly.
- [x] Agents that look for other filenames are redirected by thin stubs that carry no
      rules of their own, so nothing can drift: `CLAUDE.md`,
      `.github/copilot-instructions.md`, `.cursor/rules/wayland-vnc.mdc`.
- [x] The `.agents/skills/*/SKILL.md` runbooks assume no agent runtime, tool API or
      slash command -- audited for references to any tool-calling interface and found
      none; every step is an ordinary shell command a human could run.

## 14. Local-network default, and the service runs right after install

Two product decisions made on 2026-09-15:

- [x] **Bind to the local network, not loopback.** *Reversed below ("The fence was
      never there"): the default is loopback again and the wildcard is the settings
      app's switch.* `runtime.DEFAULT_ADDRESS` was made `0.0.0.0` so a phone on the
      same Wi-Fi could reach the server, on the belief that the systemd user unit's
      `IPAddressAllow`/`IPAddressDeny=any` fenced it to loopback plus private (RFC
      1918) and link-local ranges at the kernel level. `ServerConfig.scope` classifies a bind as loopback / local-network / public
      (via `ipaddress`, so a stray public address is shouted about in the status view),
      and `127.0.0.1` remains an explicit choice in the settings dialog.
- [x] **The service is running after install, for every logged-in user.** The only
      thing that previously kept it from starting was the missing credential, which the
      unit refused to serve without. `serve` now generates a strong random password
      (`secrets.token_urlsafe(12)`, 16 chars) and stores it mode 600 on first run, so the
      postinst starts the unit for every logged-in user with no gate, and later logins
      start it via `--global enable`. Authentication is therefore always on: a random
      strong password is not "no password". The postinst tells the user where to see
      or change it (settings app or `wayland-vnc set-password`).
- [x] Every layer re-verified for the new contract: unit tests (219, runtime.py 100%),
      the package scenario suite (now proves self-provisioning writes a 600 credential
      with auth on and reaches exec), the settings-app e2e, and the systemd activation
      smoke, where BOTH users -- with and without a stored credential -- must end
      `is-active=active`.
- [x] A test-suite safety net was added at the same time: an autouse fixture makes a
      real `os.execv` fail the test. The change had briefly let a test that expected a
      refusal reach the real `wayvnc` binary and replace the test process.

## 15. Hardware validation on a real desktop

`scripts/run_hardware_validation.sh` runs the packaged product on a real machine with
the actual RealVNC Viewer. Containers cannot cover the installer's user-service
activation, the app drawer, the icon theme or a real network interface.

- [x] Phases, each recorded in `artifacts/hardware/<stamp>/report.json`: install
      (postinst must enable AND start), drawer (entry + both icons resolve from
      `/usr/share/icons/hicolor`), service (port 5900 served; records the backend),
      password (set through the product), e2e-loopback and e2e-lan (the real viewer
      authenticates and a genuine frame arrives), bad-password (refused), uninstall
      (purge leaves nothing and does not stop the desktop's own daemon), reinstall.
- [x] Three outcomes, not two: `blocked` records a prerequisite that cannot be met on
      that host, with the reason. Blocked never counts as a pass, and never silently
      disappears; only `failed` fails the run.
- [x] The fiddly parts are unit-tested in the repository rather than living in shell:
      `src/wayland_vnc/hardware.py` (100% covered by `tests/test_hardware.py`) holds
      the RealVNC stored-password DES obfuscation -- pinned against a known-good
      `vncpasswd` vector, so a drift that would silently break every generated `.vnc`
      is caught -- the `.vnc` builder (asserted never to contain the plaintext), the
      frame-evidence check that fails closed on a blank or "connecting" screen, the
      phase `Report`, and the keyring reader for grd's effective password.
- [x] `.agents/skills/hardware-validator/SKILL.md` documents prerequisites, how to
      run it (the session bus must be present), how to read the report, and the known
      blocked path.
- [ ] **Viewer phases are blocked on a grd host.** On Ubuntu's
      `gnome-remote-desktop 50.2+vnc`, a password stored via `grdctl vnc set-password`
      is not accepted by the running server for a standard VncAuth handshake --
      verified by setting a known password, restarting the daemon, and offering both
      the raw string and the GVariant-quoted form the keyring actually holds (the
      secret is stored wrapped in single quotes); both were refused with "password
      check failed". Until that is understood, prove the viewer path on a wayvnc
      backend: a wlroots desktop, or the KVM Hyprland guest, which has passed with
      this exact viewer.
- [x] First run on the maintainer's GNOME laptop (2026-09-15): install, drawer,
      service, password, uninstall and reinstall all passed; the three viewer phases
      blocked as above. That run recorded `result=passed`, which was wrong: blocked
      phases proved nothing, so the report now reports `incomplete` unless every phase
      passed, and a re-run on a grd host would read `result=incomplete`.

## 16. RealVNC connection-file obfuscation, and a GNOME VNC crash

Three findings from driving the actual viewer against a real desktop.

- [x] **The .vnc obfuscation key was wrong.** `hardware.obfuscate_password` was pinned
      against `artifacts/desktop-viewer/password.bin`, which is TigerVNC `vncpasswd`
      output for a *server-side* password file. RealVNC's `.vnc` `Password=` field uses
      the classic VNC key with DES's per-byte bit reversal
      (`e84ad660c4721ae0`, not `17526b06234e5807`). Every connection file we generated
      was silently refused. The vector is now pinned against
      `artifacts/desktop-viewer/hypr-guest.vnc`, a file the actual viewer really did
      authenticate with, and the viewer now authenticates against a live server.
      Lesson recorded: a round-trip check that encrypts and decrypts with the same key
      proves nothing; pin to an artefact the real client accepted.
- [x] **Crash located and worked around on the host.** The segfault is in
      `grd-session-vnc.c` right after `rfbProcessEvents()`: the handler reads
      `rfb_client->state` after `clientGone` has cleared `rfb_client`, so every
      disconnect -- a viewer closing, a wrong password, a timeout -- kills the daemon
      (kernel: `segfault at 50 ... in gnome-remote-desktop-daemon`; the load is the
      instruction after the `rfbProcessEvents@plt` call). The project's patch
      `0001-client-lifetime.patch` guards exactly that path. On a host whose stock
      daemon has the bug, `scripts/install-private-grd.sh` installs the build from
      `docker/Dockerfile.gnome` under `/opt/wayland-vnc` (where `backends.py` already
      points) and a user drop-in runs it in place of the distribution daemon; the
      distribution package is untouched and the drop-in is the whole undo. Verified
      on this laptop: two consecutive 15-second RealVNC-style sessions with 30 update
      requests each, both closed by the client, zero daemon restarts, where the
      stock daemon crashed on every close.
- [x] **Shipped as a package: `wayland-vnc-grd`.** `scripts/build_grd_package.sh`
      builds the `grd-build` stage of `docker/Dockerfile.gnome` and packages exactly
      that tree as an amd64 deb: `/opt/wayland-vnc/{grd,libvnc}` plus a system-wide
      user drop-in (`/usr/lib/systemd/user/gnome-remote-desktop.service.d/`) that
      runs the private daemon in place of the distribution one. Library
      dependencies are read off the binaries with `ldd` inside the build container
      and mapped to packages, so the deb declares what it links; the distribution
      `gnome-remote-desktop` stays installed and removing the package puts it back
      (maintainer scripts reload and restart the unit for logged-in users). Built
      by the release workflow as the `grd-deb` kind and smoked in a clean Ubuntu
      container (`scripts/run_grd_package_smoke.sh`: dependencies resolve, the
      daemon links the private LibVNCServer and runs, the drop-in lands, purge
      leaves nothing). `wayland-vnc` suggests it. This laptop now runs the packaged
      daemon rather than the hand-installed copy.
- [x] **Two more daemon defects found with the actual phone, both patched.**
      `0005-advertised-depth.patch`: LibVNCServer announces a BGRX framebuffer as
      depth 32 while its ZRLE encoder emits 3-byte pixels; a viewer that keeps the
      server's format (RealVNC for Android does, the desktop viewer negotiates depth
      24 first) expects 4-byte pixels, drops the stream about 200 ms after the first
      frame and retries for ever. The daemon now announces depth 24, and a
      tile-by-tile parse of the ZRLE stream is clean under both formats.
      `0006-queued-dead-connections.patch`: VNC allows one session and queues the
      rest, but the queue was pruned only for connections closed on our side, so a
      client that gave up while waiting was "accepted" later as a dead session that
      blocked every real client behind it -- a phone that "loops, then connects" and
      finally "does not connect at all". Queued peers that hung up are now peeked
      and dropped; a phone-free reproduction (live session, a queued attempt that
      gives up, then a third client) goes from no banner in 40 s to a banner in
      0.11 s. Three consecutive phone sessions afterwards held cleanly.
- [x] **The last "loops a few times, then connects" cause, from a packet capture.**
      Decoding every phone connection showed the drop always followed the phone's
      switch from its 8-bit probe format to 32-bit: the next update it received
      was still 8-bit (ZRLE tiles parse only with 1-byte pixels), it decoded
      garbage and reset the connection; the attempt that held was the one whose
      switch fell between frames. LibVNCServer read one client message per loop
      iteration and sent a pending update in between, so a `SetPixelFormat`
      followed by an update request in the same packet produced an update in the
      old format. `patches/libvncserver/0002-drain-client-messages.patch` drains
      every message already on the socket before returning to the send path. A
      replay of the phone's exact sequence went from a stale 8-bit update on
      nearly every attempt to eight clean attempts out of eight.
- [x] **Password drift on GNOME is closed.** The keyring entry is what the daemon
      checks and what the window shows, and it can change behind our back (GNOME
      Settings, a keyring write that did not survive a re-login). `serve` now
      copies the keyring value into the credentials file when they differ, so the
      CLI, the window and every viewer told the stored password agree.
- [x] **The fence was never there.** systemd applies `IPAddressAllow` only from a
      privileged manager; a user manager logs "unit configures an IP firewall, but
      not running as root" and attaches nothing (`bpftool cgroup list` on the unit's
      cgroup: empty, while a system unit with the same lines shows
      `sd_fw_ingress`/`sd_fw_egress`). So the `0.0.0.0` default had been reachable
      from every network the machine was on, and every "blocked at the kernel level"
      claim was false. Now: `runtime.DEFAULT_ADDRESS` is `127.0.0.1`; the settings app
      has one **Local Network Access** switch (`Actions.set_lan_access`, also
      `provision --address 0.0.0.0`) that binds every interface and restarts the
      running server; the status row and the switch say plainly that this reaches
      every network the machine is on; the inert `IPAddressAllow` lines are gone from
      both units. GNOME: the private daemon gained
      `patches/gnome-remote-desktop/0007-listen-address.patch`
      (`WAYLAND_VNC_LISTEN_ADDRESS`, empty = upstream's every-interface listener),
      driven by a user drop-in `serve` and the switch write; with the distribution's
      daemon the switch is insensitive and says why. The hardware run asserts the
      loopback default, opts in, connects over the LAN, opts out, and proves the LAN
      address then refuses.
- [x] **The unit's mount sandbox broke serve on GNOME, silently, for hours.** In a
      user unit every mount-namespacing directive (`ProtectSystem`, `ProtectHome`,
      `ReadWritePaths`, `PrivateTmp`) means an unprivileged user namespace, and
      Ubuntu's AppArmor profile for those (`unprivileged_userns`, enforced by
      `kernel.apparmor_restrict_unprivileged_userns=1`) forbids reading other
      processes' fd tables -- while none of the promised read-only mounts is applied
      (`/` stays rw, `$HOME` writable). `listener_pid()` therefore answered 0 from
      inside the unit, `serve` reported "port 5900 stayed held by process 0" after
      its 70 s wait, and systemd restarted it every 75 s -- 250 times on this laptop
      in one day -- while the hardware run's "unit is active" check kept passing
      inside that 70 s window. The four directives are gone from both units
      (`NoNewPrivileges` stays; it is real), the tests pin their absence, and the
      hardware run now waits for the unit to SETTLE: serve returned (active/exited)
      or still running after the wait, with `NRestarts=0`.
- [x] **The desktop viewer lost the session at its 8-bit-to-32-bit switch.** Found by
      the same hardware run, on loopback: "bad rectangle: 65535x65535 at
      65535,65535" 12 ms after "Using pixel format depth 24". A tcpdump of the
      session, decoded rectangle by rectangle, showed the server's bytes correct in
      the new format and the viewer decoding the RichCursor pseudo-rect that led the
      update in the OLD format, then reading white cursor pixels as a header. It is a
      request-bookkeeping fault in RealVNC Viewer, triggered by how LibVNCServer
      answers the first request after GNOME Remote Desktop resizes its 1920x1080
      placeholder to the real screen: a FramebufferUpdate holding only the
      DesktopSize rectangle, then the requested content as a second update no
      request matched. The viewer counts the first as its reply and the second as
      unsolicited, and after its format switch decodes one more update in the old
      format. Proven with a TCP proxy altering one thing at a time (with
      `TCP_NODELAY`, since Nagle in the proxy had itself changed the outcome): merging
      the size-only update into the next one passed 7/7, the unaltered stream failed
      every time; a first hypothesis (the cursor rect flushed on its own) was wrong,
      shown by a rebuild that changed the delivery and not the outcome.
      `patches/libvncserver/0003-size-change-in-update.patch` sends the size
      rectangle inside the update that carries the requested content: one reply per
      request. The history shows no loopback run had ever passed against this daemon
      (the 09-14 "passed" runs had the phase BLOCKED, before blocked stopped counting
      as a pass); only the phone, which negotiates differently, ever worked.
- [ ] **GNOME Remote Desktop's VNC crashes on connect (upstream).** On Ubuntu 26.04's
      `gnome-remote-desktop 50.2+vnc`, the daemon is killed by a signal on every
      inbound VNC connection: systemd logs
      `gnome-remote-desktop.service: Failed with result 'signal'` and increments
      NRestarts each time. Reproduced 3/3. It is not client-specific -- the actual
      RealVNC Viewer (which reports `Protocol error: bad xrle data` as the stream
      dies) and a minimal RFB 3.8 client that only completes VncAuth both trigger it.
      Consequence: VNC is not usable on this GNOME build; a phone authenticates and
      then loses the session. The crash is upstream, not in wayland-vnc, which
      delegates to that daemon because it is the only VNC server Mutter can feed.
      Evidence: `artifacts/desktop-viewer/gnome/grd-crash-*/`. No matching upstream
      report exists: the closest, gnome-remote-desktop issue 96 (2022, version 42 on
      arm64, RDP and VNC), is a different crash on a different build. An upstream
      report with this evidence is the next step and needs a GNOME GitLab account.
      This is why GNOME needs the project's own private GRD build
      (`backends.py` points at `/opt/wayland-vnc/grd/...`) rather than the distro one,
      and why the hardware run's viewer phases cannot pass on a stock GNOME host.
- [x] **The daemon was SIGKILLed mid-stream at 4K.** RealVNC Viewer 7.8.0 for
      Windows (the AMD/NVIDIA lab PC, wired LAN) against this laptop's 3840x2160
      session: authentication, then `code=killed, status=9/KILL` a few seconds into
      streaming, five sessions out of six. Not the viewer: libpipewire's `module-rt`
      has rtkit make the stream's data loop `SCHED_RR` and sets `RLIMIT_RTTIME` to
      rtkit's 200 ms ceiling, the private profile takes the MemFd path (DMA-BUF off),
      so that thread copies 33 MB per frame, and once copies overrun the frame
      interval it never sleeps -- the kernel's realtime watchdog then kills the
      process. Sampled on the thread: 239 ms of CPU in 250 ms of wall time right
      before the kill. Neither the unit nor the environment can change what the
      library does (`PIPEWIRE_PROPS` does not reach module conditions;
      `PIPEWIRE_CONFIG_DIR` replaces the whole search path), so
      `patches/gnome-remote-desktop/0008-pipewire-data-loop-not-realtime.patch`
      creates the PipeWire context with `module.rt = false`, the documented
      condition under which `client.conf` skips `module-rt`. Eight sessions of eight
      then completed, each with 275-522 ms stretches that would have been a kill.
      Report: `docs/upstream/gnome-remote-desktop-03-realtime-data-loop-killed.md`.
      The installed private daemon picks this up with `scripts/install-private-grd.sh`
      after `scripts/fixture-smoke.sh gnome` rebuilt the image.

## 17. Qualification run against the actual RealVNC Viewer

Ran the unattended scenario suite through the isolated viewer harness on every
buildable wlroots target (2026-09-15), after the connection-file obfuscation fix in
section 16 -- which is what had been silently refusing every generated `.vnc`.

| target | status | scenarios |
| --- | --- | --- |
| sway | incomplete | 16/17 passed |
| wayfire | incomplete | 15/17 passed |
| xfce-labwc | incomplete | 15/17 passed |
| lxqt-labwc | incomplete | 15/17 passed |

Everything a container *can* exercise passes with the real viewer: first-frame,
colours, changing frames, keyboard, pointer, scroll, drag, resize, 4k-200, 1080p-100,
lock, reconnect-20, network-interruption, server-restart, viewer-killed.

- [x] **A VM-backed driver exists, and suspend-resume passes on it.**
      `qualify-desktop.py --kvm` drives the KVM Hyprland guest through the QEMU guest
      agent (`src/wayland_vnc/qemu_guest.py`); it is the only driver that claims
      `supports_suspend`. The scenario puts the machine through real ACPI S3 with the
      actual RealVNC Viewer connected, requires QMP to report `suspended` and then
      `running`, and then requires the desktop to reach the viewer again; the
      hypervisor transcript is kept as evidence. Run against the guest it passes
      first-frame, colors, changing-frames, 1080p, reconnect-20, viewer-killed,
      network-interruption (the whole VM is stopped and continued), server-restart
      and suspend-resume, with the post-run smoke green (one run per guest boot:
      virtio-gpu cannot reopen its DRM device after S3, so the driver refuses a
      second run on a suspended guest rather than failing halfway). The record is still
      `incomplete`, honestly: the guest has no input harness yet (keyboard, pointer,
      drag, scroll, lock are `not-run`), and Hyprland is not runner-resizable
      (`resize`, `4k-200`, `monitor-change`).
- [ ] **No target can reach `passed` yet, and here is exactly what is left.** For a
      record to validate, every scenario must pass on the same fixture. The container
      fixtures have input, lock, resize and (on Sway) monitor-change but cannot
      suspend; the KVM guest can suspend but has none of the rest. Closing the gap is
      one of two things per target: a VM image for each target that also hosts the
      input harness (the viewer harness container can run inside the guest, on the
      guest's own network), or a suspend path for containers, which does not exist.
      The first is the honest route and is now plain engineering: the KVM driver,
      the guest provisioning and the suspend scenario are done and tested; what
      remains is an image per target and the harness inside it. Until then
      `qualification/records.json` stays empty and releases are prereleases.
- [x] **The gate can now read evidence** (2026-09-17). Until today the release
      workflow's gate could only ever answer "no evidence": the evidence lives on the
      trusted runner and nothing carried it to the hosted job. `scripts/publish-evidence.py`
      (module `wayland_vnc.evidence_store`) copies one commit's records and their
      hash-verified artifacts into a private git store laid out per commit like an
      evidence root; the gate checks that one directory out read-only with a deploy
      key and runs the unchanged `check-release.py` against it, still failing closed
      to a prerelease when the store is unreachable or empty for the commit. The
      maintainer's one-time steps (private repository, read-only deploy key, the
      `EVIDENCE_DEPLOY_KEY` secret) are in [evidence-store.md](evidence-store.md).
      What is missing is now only the evidence itself.
- [x] **The Android viewer is driven unattended** (2026-09-17). `scripts/qualify-desktop.py
      --viewer android` runs the same scenarios against the same container fixtures
      with the actual RealVNC Viewer for Android in the isolated AVD
      (`wayland_vnc.android_viewer`): the app's connection screens are answered from
      UI dumps; the relative, accelerated pointer is placed closed-loop against the
      scene's pointer report and clicked; typing is forwarded; drag is the app's
      double-tap-and-hold; scroll is its two-finger swipe injected as raw multi-touch
      through the emulator console; the first frame is timed from the moment the app
      asked the server. Established against the real app, not assumed: on the Sway
      fixture keyboard, pointer, drag and scroll all produced the scene's markers.
      Provisioning: `scripts/android-provision-viewer.sh` installs the APK from a file
      or URL -- a third-party mirror is permitted (maintainer's decision, 2026-09-17)
      -- but refuses anything not carrying RealVNC Ltd's pinned release signature; a
      tampered copy fails the integrity check. Full records per target are the next
      step (the first complete Sway run is in progress as this is written), then the
      remaining six targets and the VM route for `suspend-resume`.

## 18. Translations for every language Ubuntu-Hello supports

The settings window is the part of wayland-vnc a person reads, so it is translated.
The CLI's JSON output deliberately is not: those payloads are a machine-readable
contract that scripts parse, and translating them would break callers.

- [x] **101 languages: the whole Ubuntu-Hello list.** The first 30 covered every
      inhabited continent; the rest is the complete `po/whisper-languages.txt` set
      from Ubuntu-Hello (98 codes) plus the pt_BR, zh_CN and zh_TW variants that
      predate it. Nothing was removed to make room. The catalogues were authored by
      the coding agent, not by native speakers: each is complete and placeholder-safe
      (the lint proves that), but wording in the smaller languages has not had a
      native review, and corrections are welcome as ordinary PRs against `po/`. Each
      language is listed in the window by its endonym -- the name the language uses for itself --
      which is what a speaker recognises when the interface is in a script they
      cannot read.
- [x] **Automatic is the default.** A desktop already knows what language its owner
      reads, so the window follows the session's locale unless someone deliberately
      chooses otherwise. The choice is stored in `preferences.json` beside the
      credentials and applied before the first frame is drawn, so opening the window
      never flashes English and then re-renders.
- [x] **Only installed catalogues are offered.** `i18n.available()` lists what is
      actually compiled under `share/locale`; with none present the row is
      insensitive and the group says why, rather than offering entries that would
      silently do nothing.
- [x] **Right-to-left is handled.** Arabic, Persian, Hebrew, Urdu, Pashto, Sindhi
      and Yiddish mirror the window through
      `Gtk.Widget.set_default_direction`, both when chosen explicitly and when the
      session locale selects them.
- [x] **Gated like code.** `scripts/build-translations.sh check` fails if the
      template is stale against the marked strings or if any catalogue does not
      compile, and it runs in both the local pipeline and CI. Tests assert that every
      advertised language ships a catalogue, that no message is missing or blank in
      any of the 101, and that every `%s`/`%(name)s` placeholder survives translation
      -- a dropped placeholder would crash the window at render time.
- [x] **Shipped by every package kind.** `stage-payload.sh` compiles the catalogues
      into `share/locale`, so deb, rpm, arch, AppImage, Flatpak and Snap all carry
      them; `i18n.locale_dir()` resolves them relative to the installed module, with
      `WAYLAND_VNC_LOCALEDIR` for relocatable bundles.
- [x] **Compiling needs no gettext.** Packaging runs in minimal containers -- Fedora,
      AlmaLinux, openSUSE, Arch, and the Flatpak and Snap sandboxes -- that carry
      Python but not the gettext tools, and shelling out to `msgfmt` failed every one
      of those builds. `scripts/compile_catalogue.py` writes the binary catalogues
      instead, so packaging depends only on Python, which the payload already needs.
      It is tested against msgfmt itself: all 101 catalogues must compile to the same
      messages the reference implementation produces. `msgfmt --check` stays in the
      lint gate, where gettext is installed.
- [ ] **Native review is still wanted.** The strings were translated for this release
      and are complete and placeholder-correct, but they have not been reviewed by
      native speakers of each language. Corrections are ordinary pull requests
      against `po/<lang>.po`.

## 19. arm64

Requested 2026-09-17: the product must work on arm64, tested under Docker and in CI.
What was true before: the `wayland-vnc` deb (`all`) and rpm (`noarch`) already ran
anywhere their distribution ships `wayvnc`, but the GNOME backend was built and
packaged for amd64 only, the AppImage embedded an x86_64 runtime, and nothing had ever
executed on arm64.

- [x] **One place spells the architecture.** `scripts/target-arch.sh` answers
      `deb|platform|appimage` for the host, or for `WAYLAND_VNC_ARCH` /
      `DOCKER_DEFAULT_PLATFORM` when a run is emulated. `build_grd_package.sh`
      builds and packages `wayland-vnc-grd_<v>_<arch>.deb` for it (explicit
      `--platform` on every docker call), the AppImage build and smoke pick
      `appimagetool-<arch>` with a pinned checksum per architecture and name the
      output `-x86_64` or `-aarch64`, and the GRD deb smoke looks for the
      container's own `dpkg --print-architecture`. The pinned `ubuntu:26.04` digest
      is a multi-arch manifest list, so the fixture images build unchanged.
- [x] **CI runs the container jobs on both architectures.** Fixture smoke, package
      smoke (deb, rpm; the official Arch image is x86_64-only), service activation and
      the portable smokes gained an `arch` axis on `ubuntu-24.04-arm` runners; the
      release workflow builds `grd-deb`, `appimage`, `flatpak` and `snap` on an arm64
      runner as well and names the artifacts per architecture.
- [x] **Verified under emulation on this laptop** (qemu-user via Ubuntu's `qemu-user` and
      `qemu-user-binfmt`, 2026-09-17): `wayland-vnc-grd_1.0.0_arm64.deb` builds
      (ELF aarch64 daemon linking the private LibVNCServer) once LeakSanitizer is
      disabled for the emulated run only -- LSan cannot run under qemu-user and aborts
      the encoding test, so `test-encoding.sh` turns it off when the build passes
      `WAYLAND_VNC_EMULATED=1`, and never natively; the deb package smoke passed on all
      three distributions in arm64 containers.
- [ ] **Not shown under emulation, by its nature:** the service-activation smoke
      (systemd as PID 1) fails under qemu-user with `loginctl enable-linger:
      Connection timed out` -- logind's D-Bus round trips do not fit their timeouts
      when every instruction is emulated; and the AppImage smoke cannot even start
      the tool: an AppImage carries `AI\x02` in the ELF identification padding that
      the qemu-user binfmt mask requires to be zero, so the kernel refuses to hand it
      to the emulator (`Exec format error`). Both are the native arm64 CI runner's to
      make, not the laptop's. The fixture smoke under emulation exposed one real
      blind spot instead: every emulated process has `qemu-aarch64` as argv[0] and
      the program as argv[1], so `fixture_smoke.named()` counted no compositor;
      it now looks past a user-mode interpreter (unit-tested), and emulated fixture
      images are tagged `:dev-<arch>` so they can never replace the native image --
      which they did once, putting an arm64 fixture into an amd64 qualification run.
- [ ] **Not yet shown:** no arm64 CI run has completed, and no arm64 hardware or
      viewer run exists. Until CI is green on the arm runners the README keeps arm64
      as "built and smoke-tested, not qualified".

## Invariants (do not violate)

- No suppressions of any kind (no `# noqa`, no per-file-ignore config).
- Loopback default (bind 127.0.0.1); the local network is an explicit opt-in (the
  settings app's Local Network Access switch, bind 0.0.0.0, which a user unit cannot
  fence); never the Internet, never unauthenticated.
- Never enable/start the service on the host during development; test in containers.
- Connection is not compatibility; only qualified evidence marks a target supported.
- `artifacts/` (credentials, evidence) stays Git-ignored; never print the password.
