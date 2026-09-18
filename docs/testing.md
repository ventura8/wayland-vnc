# Testing and evidence

The unit suite is not desktop qualification. Run `scripts/build-and-test.sh --unit`
for the quick loop, `--full` for tooling checks, and `scripts/test-native.sh` for
the ASan/UBSan buffer-layout regression. Native helper tests do not establish daemon
integration or viewer correctness.

The full suite also generates all three backend units, verifies them with
`systemd-analyze`, uninstalls them, and fails if any staged file remains.

## Isolated Sway fixture

```sh
docker build -f docker/Dockerfile.sway -t wayland-vnc-sway:dev .
docker run --rm --name wayland-vnc-sway --cap-drop ALL \
  --security-opt no-new-privileges -p 127.0.0.1:5901:5900 wayland-vnc-sway:dev
```

This creates its own headless compositor with XWayland disabled and a native GTK
synthetic scene. It does not mount any host desktop socket or personal files.
The random disposable password is in the container's private runtime config; copy
it privately when provisioning a viewer. Never publish that config as an artifact.
The desktop viewer connects to loopback port 5901. Android requires a separately
isolated network path; do not expose the fixture publicly to make it reachable.

For stable manual reruns, mount a previously copied private fixture configuration
read-only and point `WAYLAND_VNC_CREDENTIAL_FILE` at it. The fixture imports only
its `username` and `password`; it regenerates the server key and remaining config.
The pinned image's fixture account uses UID 1000 so a mode-`600` secret owned by
the laptop's UID 1000 remains readable without weakening its permissions.

Starting this fixture is not a passing E2E test. Automated viewer pixel/input
assertions, the other six desktop fixtures, Android instrumentation and full
qualification orchestration remain required implementation work.

## Isolated labwc foundation

`docker/Dockerfile.labwc` runs real labwc 0.9 with the native Wayland scene and
WayVNC. Ubuntu's labwc package depends on `xwayland` and exits when no Xwayland
executable exists, so the image replaces `/usr/bin/Xwayland` with the fail-closed
stub `tests/fixtures/labwc/Xwayland-disabled`; lazy XWayland activation can never
start an X server. labwc still exports `DISPLAY` to autostart children, so the
autostart launches the scene through `env -u DISPLAY`. Verification requires no
Xwayland process, no usable Xwayland executable, an unset `DISPLAY` in the scene
process, `GDK_BACKEND=wayland`, and a captured 1920x1080 output. This is the
compositor foundation for the Xfce+labwc and LXQt+labwc targets; labwc alone does
not qualify either desktop session.

```sh
docker build -f docker/Dockerfile.labwc -t wayland-vnc-labwc:dev .
docker run --rm --cap-drop ALL --security-opt no-new-privileges \
  -p 127.0.0.1:5902:5900 wayland-vnc-labwc:dev
```

Actual RealVNC Viewer 7.15.1 has connected to this fixture over RA2-256/AES-256,
rendered the 1920x1080 RGBW scene, produced changing frames, and delivered
keyboard and pointer input that the native scene acknowledged. An invalid
credential was rejected before the successful attempt. That private evidence is
partial compositor evidence only.

## Genuine Xfce and LXQt sessions on labwc

`docker/Dockerfile.xfce-labwc` starts the distribution's own `startxfce4 --wayland`,
which runs `labwc --session xfce4-session`; `docker/Dockerfile.lxqt-labwc` starts
`startlxqtwayland` with `compositor=labwc` in `session.conf`, which runs
`labwc -S lxqt-session`. The synthetic scene is launched by the desktop session
itself through an XDG autostart entry, not by the fixture supervisor. Smoke
checks require the real session processes (`xfce4-session`, `xfsettingsd`,
`xfce4-panel`, `xfdesktop`; `lxqt-session`, `lxqt-panel`), `XDG_CURRENT_DESKTOP`
naming the desktop, and a Wayland session type. Both images carry the same
fail-closed Xwayland stub as the labwc fixture. Actual RealVNC Viewer 7.15.1 has
passed the colors, changing-frames, keyboard, and pointer checks against both
sessions and against Wayfire; `scripts/record-partial-evidence.py` summarizes
such a private run with content hashes, always with status `incomplete`.

## Isolated GNOME path

`docker/Dockerfile.gnome` builds the private LibVNCServer 0.9.15 (ZRLE preference
patch) and the private GNOME Remote Desktop 50.2 VNC daemon (patches 0001-0004)
from the checksum-verified archives in `sources.json`, links the daemon to the
private library through its RPATH, and installs both under `/opt/wayland-vnc`.
The runtime stage runs real `gnome-shell --headless --wayland --no-x11
--virtual-monitor 1920x1080` (Mutter's surfaceless software renderer, no GPU),
PipeWire and WirePlumber. GNOME Shell 50 requires a system bus, so the fixture
starts a private, empty one (`tests/fixtures/gnome/system-bus.conf`) and points
`DBUS_SYSTEM_BUS_ADDRESS` at it; `/run/systemd` is removed from the image so the
shell uses its non-logind login manager. VNC settings come from a GSettings
keyfile backend, and the password reaches the daemon only through upstream's
`GNOME_REMOTE_DESKTOP_TEST_VNC_PASSWORD` environment override, never argv.
`grdctl` and the daemon never touch the host's own GNOME Remote Desktop.

GRD VNC offers classic VncAuth only, so RealVNC connects unencrypted after its
warning; the fixture is loopback-only and this remains a documented limitation
of the GNOME backend. Actual RealVNC Viewer 7.15.1 has passed the colors,
changing-frames, keyboard, and pointer checks against this fixture, with the
private library selecting ZRLE.

### The distribution daemon crashes when a session ends

On a real GNOME host the same crash makes a phone's RealVNC Viewer loop in
"attempting to reconnect": every disconnect (including a wrong password) kills the
daemon, systemd restarts it, the viewer retries. `scripts/install-private-grd.sh`
puts the private build in front of the distribution daemon on that host through a
user drop-in; see the packaging plan, section 16, which also records two further
defects that only the actual phone exposed (the announced colour depth, and dead
connections left in the daemon's single-session queue) and their patches.

Ubuntu's `gnome-remote-desktop 50.2-0ubuntu0.1+vnc3+vnc44` carries a downstream VNC
patch whose daemon segfaults every time a VNC session ends, however the session ends:
an abrupt disconnect right after the RFB greeting and a fully authenticated session
closed gracefully both kill it. It also serves one session at a time, and a daemon
that somehow survives a session stops answering new ones -- it accepts the TCP
connection, never sends the RFB greeting, and leaks the socket in `CLOSE-WAIT`.

Two consequences shaped the code, both observed on hardware:

- **Readiness must never open a VNC connection.** An earlier `backend_is_serving`
  connected and waited for the RFB greeting. That probe *was* a session, so it
  crashed the daemon it was checking, and the user's next connection was refused --
  the exact "first attempt fails, second works" report that prompted the fix.
  `backend_is_serving` now asks `systemctl --user is-active` and opens no socket.
- **systemd's default start limit turns the crash into an outage.** At five starts in
  ten seconds systemd refuses to start the unit at all; a handful of ordinary
  reconnects left the desktop with no VNC server for minutes. `serve_grd` installs a
  user drop-in (`~/.config/systemd/user/gnome-remote-desktop.service.d/`) widening the
  limit to twenty starts, and `wait_for_backend` clears a unit systemd has already
  given up on and starts it once more.

Measured on the ZenBook after the fix: eight rapid connect/disconnect cycles, eight
sessions authenticated and served, eight upstream segfaults, zero start-limit
lockouts, daemon still active. Before it, the same pattern left the daemon dead.
The segfault itself is upstream and is not something this project can fix.

## Genuine KDE Plasma path

`docker/Dockerfile.plasma` builds TigerVNC 1.16.2 `w0vncserver` from the
checksum-verified archive pinned in `sources.json` (only the Wayland server and
`vncpasswd` targets; no X11 server is installed) and runs real `kwin_wayland`
with its virtual backend, `plasmashell`, PipeWire, WirePlumber,
`xdg-desktop-portal` and `xdg-desktop-portal-kde`. `w0vncserver` prefers the
RemoteDesktop portal when one exists, so every session start raises KDE's real
"Remote Control" consent dialog; `tests/fixtures/portal-consent.py` presses its
Approve button through AT-SPI, the way a tester would, and logs each approval.
Nothing fakes or bypasses the portal.

KWin's PipeWire screencast requires OpenGL compositing, and KWin only offers
OpenGL when a DRM device exists (KDE's own CI loads `vgem`). The fixture therefore
receives one DRM **render** node (`WAYLAND_VNC_RENDER_NODE`, default
`/dev/dri/renderD128`); the KMS `card` nodes that own the host display are never
shared. Without a render node `scripts/fixture-smoke.sh plasma` exits 3 and
prints `BLOCKED`; both `--full` and `--fixtures` report that and fail, because a
blocked check is not a passed one.
`kwin_wayland` is stripped of `cap_sys_nice` so it can start under
`no-new-privileges`, and `XDG_MENU_PREFIX=plasma-` is required for KWin to find
the portal's `X-KDE-Wayland-Interfaces` authorization.

Actual RealVNC Viewer 7.15.1 has passed the colors, changing-frames, keyboard,
pointer, and portal-approve checks against this fixture over RA2-256/AES-256.
Portal deny, revoke, and restore scenarios remain not run.

## Wayfire and Hyprland

`docker/Dockerfile.wayfire` runs Wayfire 0.10 headless with `xwayland = false` and
the scene from its `[autostart]` section; it passes the smoke checks.

`docker/Dockerfile.hyprland` builds, but Hyprland 0.53 cannot start inside the
isolated container: its aquamarine backend needs a KMS DRM device opened through
libseat or a dmabuf-capable parent compositor. The only DRM nodes on the laptop
belong to the running GNOME session and must never be handed to a fixture, and
nesting inside the pixman headless Sway parent fails with `Missing protocols`.
The Hyprland fixture therefore lives in a KVM guest instead of a container:
`scripts/kvm/build-hyprland-guest.sh` boots a disposable Ubuntu cloud image with a
virtio-gpu KMS device, installs Hyprland, WayVNC and the scene through cloud-init,
and forwards WayVNC to host loopback only. `scripts/fixture-smoke.sh hyprland` (the
container path) still fails closed; the guest runs the very same
`wayland_vnc.fixture_smoke` checks from its own copy of the tree, and passes all ten.

## KVM guests for every wlroots target

Suspend-resume needs a machine, and on real DRM outputs the mode, hot-plug and lock
scenarios mean what they say. `scripts/kvm/build-guest.sh TARGET [--harness]` builds
and boots a disposable guest for every target -- `hyprland`, `sway`, `wayfire`,
`xfce-labwc`, `lxqt-labwc`, `gnome` and `plasma` (`build-hyprland-guest.sh` is the old
name for the first). Each target has its own work directory (`artifacts/kvm/TARGET`)
and forwarded port (5920-5926,
recorded in `artifacts/kvm/TARGET/port`), so guests coexist on one host, and
`scripts/qualify-all.sh --kvm` boots a fresh guest per target and runs it through the
harness viewer. The wlroots guests are the container fixture on a virtio-gpu KMS
device: cloud-init (`tests/kvm/wlroots.user-data.template`, rendered by
`scripts/kvm/render-user-data.py` with the package list taken from the target's own
Dockerfile) installs the same distribution packages, copies `tests/fixtures`,
`src/wayland_vnc` and the tree into the guest, and a system unit runs
`tests/fixtures/sway-session.py` as the `fixture` account with `WLR_BACKENDS=drm,headless`.
What the guests taught us, and what the template encodes:

- The unit has no PAM session on purpose. seatd provides the seat; a logind session
  would move the fixture's processes into a session scope the unit's stop cannot
  reach, and the next start finds the DRM device busy. For the same reason the
  fixture's runtime directory is the unit's own `RuntimeDirectory`
  (`/run/wayland-vnc-runtime`, announced to the runner in
  `/run/wayland-vnc-guest-runtime`), not logind's `/run/user/1000`, which comes and
  goes with sessions. The session script sweeps only its own entries there.
- virtio-gpu without virgl is llvmpipe, and wlroots refuses software GLES unless
  `WLR_RENDERER_ALLOW_SOFTWARE=1`. QEMU also adds its default `stdvga` next to the
  virtio-gpu unless told `-vga none`: two DRM devices, both connectors called
  `Virtual-N` in an order that changed from boot to boot, and only the virtio-gpu one
  takes the 4K mode -- a guest whose `Virtual-1` was the stdvga head failed `4k-200`
  for exactly that reason. The builder passes `-vga none`; the compositor configs
  still disable `Virtual-2` from the start and put `Virtual-1` at the origin at
  1080p, because disabling a second head later once carried sway's first workspace,
  scene included, behind the workspace already shown, and the viewer saw black. The
  session script switches off any further output as well.
- WayVNC's `ext-image-copy-capture: No supported buffer formats were found` line on
  a guest is its cursor session (nothing to capture, `buffer_size(0, 0)`), not the
  output; the output session offers XRGB8888 and frames complete.

GNOME and Plasma have guests of their own (`tests/kvm/desktop.user-data.template`):
the same session scripts as their containers (`gnome-session.py`, `plasma-session.py`)
with `FIXTURE_DRM=1`, which puts gnome-shell and kwin_wayland on the virtio-gpu device
inside a logind session (`PAMName=login` -- Mutter and KWin take the seat through
logind, unlike the wlroots compositors with seatd) and uses the machine's own system
bus. GNOME serves through the project's private daemon installed from
`artifacts/grd-deb/wayland-vnc-grd_*_amd64.deb`; Plasma through the project's TigerVNC
`w0vncserver`, taken out of the Plasma fixture image into `artifacts/kvm/tigervnc`.
On these guests the runner's machine capabilities add what the headless containers
cannot offer: output modes and the second connector through Mutter's DisplayConfig
(`tests/fixtures/gnome/mutter-monitors.py`) or `kscreen-doctor`, and the desktops'
own lock screens (`org.gnome.ScreenSaver`, `org.freedesktop.ScreenSaver`), which the
viewer wakes with a key before typing the password. Two guest facts shape the code:
in a logind session the fixture account cannot read even its own processes' `/proc`
entries, so the smoke runs as root and hands only the Mutter query to the session bus
owner; and the guest's runtime directory is logind's `/run/user/1000`, whose sweep is
limited to the fixture's own entries.

The compositors' DisplayConfig only offers the modes the connector advertises, and
QEMU's virtio-gpu advertises either a 4K-capable set or a 720p set, never the
1080p/720p/4K the scenarios need together. So the desktop guests give the connector a
custom EDID (`scripts/kvm/make-edid.py`) through the kernel's
`drm.edid_firmware=Virtual-1:edid/wayland-vnc.bin`, which is read at DRM module load:
cloud-init writes the blob and reboots the guest once (`power_state: reboot`), and the
fixture -- and the readiness marker the runner waits for -- start on the second boot,
both gated on `ConditionKernelCommandLine=drm.edid_firmware`. Two guest quirks shape
the EDID: the connector always prunes the *first* (preferred) detailed timing of a
firmware EDID, so the blob leads with a sacrificial 1600x1200 timing and the three real
modes (4K, 1080p, 720p) follow it; and 3840x2160 has to be 30 Hz, because its 60 Hz
pixel clock (~533 MHz) is pruned as too fast while ~266 MHz at 30 Hz is accepted (the
refresh rate does not matter for a VNC capture). The fixture unit is only
`After=systemd-logind.service`, never `After=multi-user.target`: the readiness service
is ordered after the fixture and `WantedBy=multi-user.target`, so ordering the fixture
after multi-user too would be a cycle that makes systemd drop the readiness service.

`scripts/qualify-desktop.py --kvm artifacts/kvm/TARGET` drives a guest instead of a
container. The actual RealVNC Viewer runs on the host against the forwarded port;
everything fixture-side goes through the QEMU guest agent
(`src/wayland_vnc/qemu_guest.py`: `guest-exec` for commands, QMP `stop`/`cont` for
the network interruption, `systemctl restart` of the fixture unit for the server
restart). It is the only driver that claims `supports_suspend`, and
`suspend-resume` is the reason it exists: with the viewer connected the runner
issues `guest-suspend-ram`, waits until QMP `query-status` really reports
`suspended`, keeps the machine in S3 for twenty seconds, wakes it with
`system_wakeup`, waits for `running` and for the agent to answer again, and only
then looks at the viewer -- frames on the surviving session, or a clean
reconnect. The hypervisor's transcript is written beside the captures as
`suspend-transcript.json`; a machine that never reported `suspended` fails the
scenario whatever the frames show.

Two things the guest taught us. The cloud image regenerates WayVNC's RSA key on
every boot, so the viewer cannot pin an identity; the runner passes `-VerifyId=0`
for the guest and records it in the viewer version string, as it does for GNOME.
And virtio-9p does not survive ACPI S3: after a resume every process that touches
a 9p mount sits in uninterruptible sleep -- seatd, the fstab generator on the next
`daemon-reload`, anything copying logs out -- and the next compositor start blocks
for ever. Cloud-init therefore copies `src/` and `tests/` into the guest at first
boot, unmounts the share and drops it from fstab; nothing in the guest touches 9p
after that, and the runner reads the fixture journal through the guest agent. Two
more guest facts are handled: Hyprland 0.53 segfaults on exit, and writing that
core keeps DRM master busy long enough for the next instance to block, so the
fixture unit runs with `LimitCORE=0` and a bounded stop, and the driver's restart
is stop, wait for the compositor to be gone, start, wait for the socket and the
listener. The smoke check treats a guest that stops answering as a failed health
check rather than a runner crash, so the record is still written.

One limit remains and is enforced rather than worked around: virtio-gpu does not
come back whole from S3. The session that was streaming keeps streaming (which is
what the scenario proves), but the next open of the DRM device blocks in the
kernel for ever -- seatd and a virtio-gpu kworker sit in uninterruptible sleep --
so nothing can restart the compositor -- or change its mode -- after a resume.
Suspend-resume therefore runs last of all, after the mode scenarios (the machine
suspends at the mode the 4K scenario left, and the post-run smoke checks it there);
the driver marks the guest once it has been suspended, and a second run on the same
boot is refused with the reason; a run is one guest boot, and `build-guest.sh` takes
about four minutes on the laptop (under a minute on the lab PC, whose WSL2 exposes
nested KVM).

## Fixture smoke checks

`scripts/fixture-smoke.sh FIXTURE` builds the fixture image, starts a container
with no network and no published port, and runs `wayland_vnc.fixture_smoke` inside
it with a bounded timeout. The checker requires exactly one compositor process, one
Wayland socket, the WayVNC control socket and TCP listener, one native scene
process with a Wayland-only environment (`GDK_BACKEND=wayland`, `WAYLAND_DISPLAY`
naming the fixture socket, no `DISPLAY`), no Xwayland process or usable
executable, one captured output at the expected mode, and for desktop targets the
genuine session processes. The container is always removed, including on failure.
`scripts/build-and-test.sh --fixtures` runs Sway, labwc, Xfce+labwc, LXQt+labwc,
Wayfire, GNOME and Plasma, and `--full` includes them (Plasma reported `BLOCKED`, and
the gate failed, where no render node exists). A passing smoke check is fixture readiness, never viewer
compatibility evidence.

### Actual RealVNC desktop Viewer

Save a fixture-only connection from the installed RealVNC Viewer as a `.vnc`
file, set its mode to `600`, and keep it under `artifacts/` (which Git ignores).
This preserves RealVNC's private credential representation; do not invent a
password-file encoding or pass a password on the command line.

```sh
chmod 600 artifacts/desktop-viewer/fixture.vnc
scripts/realvnc-desktop-test.sh \
  --config artifacts/desktop-viewer/fixture.vnc
```

The bounded runner disables reconnects, UDP, proxying, and password-store prompts,
captures a screenshot through RealVNC's own control interface, requires ordered
RGBW pixel targets in that screenshot, writes `evidence.json`, and terminates its
entire process group. Changing-frame and remote-input assertions remain separate
mandatory gates before the result counts as qualification evidence.

After typing a non-empty nonce and clicking the acknowledgement button, run
`scripts/assert-scene.py --require-input SCREENSHOT`. Cyan and magenta marker
bands provide deterministic keyboard and pointer evidence without OCR.

## Automated desktop scenarios

`scripts/qualify-desktop.py` drives the installed RealVNC Viewer, unattended,
against one isolated fixture and writes a schema-v2 record into a private
evidence root. Unattended connections use a private `.vnc` connection file
written by `scripts/realvnc-connection.sh` (the password is obfuscated locally
with RealVNC's own key through `wayland_vnc.hardware`, fed in on stdin; nothing
is printed or passed on argv) and a private stable server key (`--server-key`) so RealVNC's pinned RA2
identity for `127.0.0.1::PORT` stays valid across containers. The viewer's own
"Authentication successful" line gates every screenshot request.

```sh
scripts/realvnc-connection.sh --credential artifacts/desktop-viewer/fixture.conf \
  --port 5902 --output artifacts/desktop-viewer/qualify-5902.vnc
PYTHONPATH=src python3 scripts/qualify-desktop.py --fixture sway --port 5902 \
  --connection artifacts/desktop-viewer/qualify-5902.vnc \
  --credential artifacts/desktop-viewer/fixture.conf \
  --server-key artifacts/desktop-viewer/fixture-rsa.pem \
  --evidence-root artifacts/qualification-evidence --commit "$(git rev-parse HEAD)"
```

Automated scenarios: `first-frame` (budget 10 s), `colors`, `changing-frames`,
`1080p-100`, `reconnect-20`, `viewer-killed` (SIGKILL, then a fresh session),
`network-interruption` (`docker pause` for five seconds; frames must resume or a
clean reconnect must succeed), `server-restart` (`docker restart`, smoke, fresh
session), and on wlroots fixtures a **live** `resize` (the connected viewer must
follow `wlr-randr` to 1280x720 without dropping) and `4k-200` (3840x2160 at scale
2 on an idle server). Every verdict comes from pixels the viewer rendered. After
the scenarios the runner smoke-checks the fixture again and records
`post_run_smoke`; a fixture that crashed during the run can never yield a
`passed` record. Keyboard, pointer, drag, scroll, monitor-change, lock and
the Plasma portal scenarios are recorded as `not-run` with the reason, and so is
suspend-resume on every container fixture; only the KVM driver can run it. For GNOME,
whose daemon offers only VncAuth, the runner passes
`-VerifyId=0 -WarnUnencrypted=0` and records that in the viewer version string;
RealVNC cannot pin an identity for that backend.

On Plasma the runner also exercises the real consent dialog: `portal-approve`
(Approve pressed, frames follow), `portal-deny` (Deny pressed, no desktop pixel
reaches the viewer, a later approval renders) and `portal-restore` (the
"allow restoring" box is ticked, `w0vncserver -RememberDisplayChoice=Always`
stores the token, and after a daemon restart the session starts with no
dialog). Each of these begins with `w0vncserver-forget` and a container restart
because the portal remembers a decision for its lifetime. `portal-revoke`
stays `not-run`: the fixture has no desktop UI to revoke a grant.

Observed and recorded: Sway 1.11 on the headless backend segfaults on output
mode changes once WayVNC 0.9.1 capture state exists (through wlr-randr and
through `swaymsg` alike; WayVNC also dropped a live client once). labwc and
Wayfire fixtures survive the same changes. Sway runs therefore fail `4k-200`
and end with `post_run_smoke: failed`; the Sway target cannot qualify until that
is fixed upstream, and the runner does not hide it.

`monitor-change` runs on every wlroots fixture. Sway creates and unplugs an output on
request (`swaymsg create_output`); labwc, Xfce+labwc, LXQt+labwc and Wayfire have no
such command, so their session starts with a second headless output
(`WLR_HEADLESS_OUTPUTS=2`) that is switched off before anything maps and switched on
and off again by the scenario through wlr-output-management -- to WayVNC a
`wl_output` appearing and going away. wlroots 0.19.2 has a trap here: a client is
told a headless head's "virtual mode" only if the head is enabled when the client
binds, and once any client holds one, later binders never get it, so enabling the
spare asserts inside the compositor (`head_send_state: found`) whenever a session
client that keeps wlr-output-management bound -- Xfce's `xfsettingsd` -- saw the
output enabled first. That was a race the runner lost two times in three. The Xfce
fixture therefore starts through `startxfce4 --wayland` with labwc's `--session`
pointed at `tests/fixtures/xfce-labwc/session.sh`, which switches the spare off and
only then starts `xfce4-session`; three runs in a row have since passed the scenario.

## Viewer harness: unattended input

`docker/Dockerfile.viewer-harness` is an isolated headless Sway session with
XWayland that hosts the **actual RealVNC Viewer binary**, mounted read-only from
the host at run time and never copied into an image or Git. The harness exposes
WayVNC virtual keyboard and pointer on a unix socket; `tests/harness/rfb-input.py`
is a minimal RFB client that types, clicks, scrolls and drags into the harness
compositor, which forwards the events to the real viewer, which sends them to the
fixture over RFB. `scripts/qualify-desktop.py --harness` runs the viewer this way
on an internal Docker network (no published ports, no host desktop, no uinput),
seeding the viewer's pinned RA2 identity (`--identities`) and its EULA state
(`--viewer-config`) from private files. `scripts/qualify-all.sh` runs every
buildable target this way in one command. With the harness the `keyboard`,
`pointer`, `scroll` and `drag` scenarios are automated and verified through the
scene's own acknowledgement bands (a warm-up key plus Backspace absorb the
first-keystroke drop a freshly focused virtual keyboard exhibits, so exact text
arrives intact); `lock` engages the real session lock (`swaylock`,
ext-session-lock — enabled as a Wayfire plugin, built in elsewhere) and unlocks
it by typing the disposable account password through the viewer; and on Sway
`monitor-change` hot-plugs and removes a second headless output while the viewer
stays connected. Fixture images leave `/etc/shadow` writable by the disposable
account so the lock can authenticate without a privileged helper; this is a
fixture-only concession. GNOME (no runner-driven lock or output control) and
Plasma (portal-revoke has no fixture UI) leave those scenarios `not-run`.

Latest harness results (all `incomplete`, none release-qualified): Sway 16/17,
LXQt+labwc / Xfce+labwc / Wayfire 15/17, GNOME 12/17, Plasma 15/21 — the only
universally `not-run` scenario on the container fixtures is `suspend-resume`,
which the KVM driver runs (and passes) on the Hyprland guest. The
`reconnect-20` scenario tolerates up to three transient RealVNC RA2 handshake
glitches (a bad-length RSA on rapid reconnect) but still fails on a genuine
refusal.

Scenario order matters: `lock` runs at 1920x1080 right after the input scenarios,
because a wlroots session stays locked if the locker exits without
authenticating, so a reliable unlock must not sit behind a later mode change.
The mode scenarios run last (`resize`, then `4k-200`), and Sway is left at 4K
because Sway 1.11 crashes on the scale-down restore even with WayVNC detached.
`suspend-resume` needs the KVM guest, and the RealVNC RA2 handshake
occasionally returns a bad-length RSA on a rapid reconnect, so `reconnect-20`
can flake; both keep the record `incomplete`, never falsely `passed`.

### The harness against the KVM guest

`--kvm artifacts/kvm/TARGET --harness` combines the two: the guest supplies the
fixture (and real suspend), the harness container supplies the viewer and the input.
The guest's VNC port is forwarded to host loopback, and the harness runs on the host
network namespace (`--network host`) so it reaches `127.0.0.1`; loopback keeps the
guest reachable only from the host, never the LAN. (An earlier design forwarded to a
docker bridge gateway, but WSL2's networking does not reliably let a container reach a
service the host binds on its own bridge gateway.) `scripts/qualify-all.sh --kvm` does
all of it per target. First exercised end to end on 2026-09-17 with the sway guest on
the lab PC.

## Other machines in the lab

Heavy or hardware-bound checks do not have to run on the development laptop. Three
machines on the LAN accept the laptop's validation key non-interactively (their
addresses and accounts are the maintainer's, kept out of this repository); the tree is
synced there with `rsync` (excluding `.git`, `artifacts/`, `vendor/`) and the same
scripts run unchanged:

- an Intel NUC on Ubuntu 26.04 with a DRM render node, Docker and KVM -- the Plasma
  fixture runs there when the laptop reports it BLOCKED;
- a Raspberry Pi 5 on Debian 13 -- real arm64: the deb and fixture smokes run natively,
  with none of the qemu-user caveats above;
- an AMD/NVIDIA PC that dual-boots Xubuntu -- real hardware (and real `rtcwake`
  suspend) for the wlroots and Xfce+labwc targets once its Linux side is set up. Its
  Windows 11 side (OpenSSH, PowerShell by `-EncodedCommand`, WSL2 Ubuntu 26.04 with
  Docker inside it) runs the unit tests, the deb smoke, the Sway fixture smoke and the
  activation smoke (all passed on 2026-09-17), and RealVNC Viewer 7.8.0 for Windows
  connects from it to the laptop's real daemon over the wired LAN -- the session that
  exposed the realtime data-loop kill
  (`docs/upstream/gnome-remote-desktop-03-realtime-data-loop-killed.md`). WSL2 stops
  the distribution as soon as the last `wsl.exe` session ends, units started inside it
  included, so a long chain there is run with the SSH session held open for its whole
  duration rather than detached.

Anything that needs `sudo` on those machines (packages, `rtcwake`) is the
maintainer's to grant; until then they run containers only.

## The Android viewer

`--viewer android` drives the actual RealVNC Viewer for Android in the isolated
emulator against the same container fixtures, through the app's own screens and
gestures; see [android.md](android.md). Everything fixture-side is shared with the
desktop viewer; only the viewer side differs, and the record says
`viewer = realvnc-android`.

## Attended input evidence

The scene acknowledges a non-empty typed nonce (cyan), a button click
(magenta), a scroll over the targets (yellow) and a drag longer than 100 px
(orange). After an attended RealVNC session on the same fixture build, capture
with `vncviewer -screenshot PID FILE` and merge:

```sh
PYTHONPATH=src python3 scripts/record-manual-input.py --record RECORD.json \
  --screenshot FILE --evidence-root artifacts/qualification-evidence \
  --commit "$(git rev-parse HEAD)" --gestures
```

The merge verifies the markers itself, stores the capture and the assertion
output as `input-results`, and lets the record become `passed` only when every
scenario passed and the fixture stayed healthy. Nothing is taken on trust.

## Required qualification

Both actual RealVNC applications must render RGB targets, changing frame IDs and
remote input acknowledgements. Check 1080p/100%, 4K/200%, first-frame latency,
twenty reconnects, interruption recovery, resize, portal consent, lock and suspend.
Record exact versions and renderer; public evidence may contain only this synthetic
scene. Missing app provisioning or test instrumentation blocks qualification.
