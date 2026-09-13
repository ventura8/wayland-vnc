# Android lab

The Android side of qualification runs the **actual RealVNC Viewer for Android** in a
dedicated, isolated emulator and drives it the way a person would, through the app's
own gestures and screens. Nothing here touches a personal Android configuration or
signs into any account.

## The isolated AVD

```sh
python3 scripts/android-lab.py --sdk /path/to/Android/Sdk     # once: create the AVD
scripts/android-stage.sh                                       # boot it, bridge the port
```

The creator writes only inside ignored project artifacts (`artifacts/android/`) and
refuses to overwrite an existing AVD. The stage script boots the isolated AVD
(`wayland-vnc-api36`, API 36, x86_64), waits for `adb` authorisation and boot, and sets
`adb reverse` so that `127.0.0.1:5900` inside the emulator reaches a fixture port on the
host. The AVD's `adb` key lives under `artifacts/android/user`; a plain `adb devices`
that does not export `ANDROID_AVD_HOME`/`ANDROID_USER_HOME` presents a different key and
sees the device as `unauthorized`. `--wipe-data` resets this AVD only.

## Provisioning the viewer: mirrors are permitted, RealVNC's signature is mandatory

RealVNC publishes no first-party APK download (their Android page links only to Google
Play), and a Play Store install needs a Google account inside the emulator. The
maintainer decided on 2026-09-17 that **the APK may be obtained from a third-party
mirror**, under one condition that no mirror can fake: it must carry RealVNC Ltd's own
release signature. An APK's v2+ signature covers the whole file, so a mirror that
changed a single byte cannot re-sign it with RealVNC's key.

```sh
scripts/android-provision-viewer.sh --url https://<mirror>/RealVNC-Viewer-4.9.4.apk
scripts/android-provision-viewer.sh --apk ~/Downloads/RealVNC-Viewer-4.9.4.apk
```

The script refuses the APK unless `apksigner` verifies an APK Signature Scheme v2 (or
later) signature by exactly one signer whose certificate SHA-256 is the pinned RealVNC
fingerprint `66d81472a2cad46121b6db13870a761425a833c93ca128c8b46a64d80307b7d9`
(`CN=RealVNC Ltd, OU=SOFTWARE, O=RealVNC Ltd, L=Cambridge, ST=Cambridgeshire, C=UK`),
and refuses a package other than `com.realvnc.viewer.android`. It then installs into the
isolated AVD and writes `artifacts/android/viewer-provenance.json` (version, ABI, file
hash, signer, source). A tampered copy is rejected by the integrity check before any of
this (verified: a one-byte change fails with `CHUNKED_SHA256 digest mismatch`).

Known-good build: RealVNC Viewer 4.9.4.60176, APK SHA-256
`87c9a052986148eee6425a12ae2b3caa72d7ea8850dc01ea0bacaaf87cfe7716`. The APK itself is
never committed (AGENTS.md).

## Running the qualification scenarios against the app

```sh
PYTHONPATH=src python3 scripts/qualify-desktop.py --fixture sway --viewer android \
  --port 5911 --credential /path/to/private/fixture.conf \
  --evidence-root artifacts/qualification-evidence --commit "$(git rev-parse HEAD)"
```

`--viewer android` selects the Android driver in the same runner the desktop viewer
uses: the fixture is the same container, every fixture-side scenario (restart,
network interruption, resize, hot-plug, lock) runs unchanged, and the record is written
with `viewer = realvnc-android` in the same schema. The driver
(`wayland_vnc.android_viewer`) is built on what the real app was observed to do:

- **Connection**: `vnc://127.0.0.1:5900` by intent; the app's "Continue connecting?",
  identity-check and Authentication screens are answered from `uiautomator` dumps, the
  username typed, the IME's *Next* used to reach the password field (in landscape the
  soft keyboard is full-screen, so taps on the fields do nothing), the password typed
  over `adb shell`'s stdin (never on the host command line), Back, CONTINUE. The app
  counts as connected once its toolbar has been seen or several consecutive dumps show
  nothing left to answer with the desktop activity in front.
- **Captures**: `screencap` of the 1920x1080 landscape screen, which shows the
  1920x1080 desktop 1:1 at the origin; the scene checks are the desktop viewer's.
- **Pointer**: the app's pointer is relative (trackpad-style) and accelerated; a tap
  clicks where the pointer is. The fixture's scene reports where the pointer last
  landed (`$XDG_RUNTIME_DIR/pointer`) and the driver swipes toward the target in two
  speed classes with learnt gains until it is within six pixels. Swipes stay inside a
  safe zone clear of Android's edge gestures and the app's toolbar, and no positioning
  swipe is ever short enough to be read as a tap (a tap would be a click).
- **Keyboard**: `input text`; the app forwards hardware key events without its own
  keyboard open.
- **Drag**: the app's double-tap-and-hold gesture, issued as one `adb shell` script so
  the double-tap window is met.
- **Scroll**: the app's two-finger swipe, injected as raw multi-touch slots through the
  emulator console (`adb emu event send`), since `input` drives one pointer and the
  Play Store image has no root for `sendevent`. The console's touchscreen frame is the
  natural (portrait) one: `nat_x = 1080 - y`, `nat_y = x`, scaled to 0..32767.
- **First frame**: timed from the moment the app was last told to go ahead (credentials
  submitted or the last dialog answered), not from the automation's typing; the
  automation time is recorded in the per-connection `viewer-NNN.log`.

`suspend-resume` is `not-run` against a container, exactly as for the desktop viewer;
it needs the virtual-machine fixture, and the app gets one the same way the desktop
viewer does: `scripts/qualify-all.sh --android --kvm` (or `qualify-desktop.py --viewer
android --kvm artifacts/kvm/TARGET`) boots the target's KVM guest with its VNC port on
host loopback and points `adb reverse` at it, so the emulator, the guest and the
runner all live on the one machine that has the emulator
(docs/testing.md, "KVM guests for every wlroots target").

## History

An earlier, manually driven session (2026-09-14) had already connected the app to the
KVM Hyprland guest and recorded partial evidence under
`artifacts/desktop-viewer/android/`; the app had been supplied as a local file and its
signature verified the same way. That work is superseded by the driver above.
