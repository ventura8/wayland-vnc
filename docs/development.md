# Development

The packages install a working product: a hardened systemd **user** unit that runs
WayVNC inside the logged-in Wayland session, enabled and started at install, with a
random viewer password generated on first start. `serve` fails closed on anything but
a native Wayland session and on a backend the capability probe did not select.

What is *not* settled is qualification. `doctor` collects capabilities and names a
backend candidate, and a candidate is not a supported configuration: no desktop is
release-qualified until it has the evidence the suite demands, and
`qualification/records.json` is still empty. Connecting successfully is not
compatibility -- see [testing.md](testing.md) for what evidence a target must produce.

## Environment

All Python work happens in the project's virtual environment, never in the system
interpreter (Ubuntu marks it externally managed, and the pinned versions in
`requirements-dev.txt` are what CI runs):

```bash
scripts/ensure-venv.sh                       # creates .venv, installs the pins; idempotent
export PATH="$(scripts/ensure-venv.sh):$PATH"   # for commands run by hand
```

`build-and-test.sh` and every host-side script under `scripts/` do this themselves.
The venv sees the distribution's site-packages, so PyGObject and the GTK4/libadwaita
typelibs come from `apt` (`python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1`),
and the settings-app tests need `xvfb` -- without it they error out rather than skip.
The same applies on a lab machine: `python3 -m venv --system-site-packages` there too,
never `pip install --user`.

Run `./scripts/build-and-test.sh --full` before submitting changes. Missing required
tools fail the full suite. `--unit` runs the smaller developer loop only and does not
qualify a release. Source dependencies and images must be pinned before publication.
Every change carries its documentation: the `docs/` page, the skill, the README line
or the release note it affects is updated in the same change set (AGENTS.md).

## Safety and evidence

Do not attach fixtures to the personal session, restart its services, or reuse its
Android AVD. Use synthetic desktops and disposable test credentials. Never publish
APKs, passwords, personal screenshots, or raw host logs. No fake portal in E2E.

Native changes belong in separate correctness and workaround patches. Build them
against checksum-verified upstream sources. Test cursor disconnect, descriptor
cleanup, invalid buffer metadata, and encoding renegotiation under sanitizers.

Full RealVNC Android and desktop tests are mandatory for stable release. Missing
fixture infrastructure, APK provisioning, or hardware tests block qualification;
they are not skips. Public PR CI must not execute on the personal laptop.

## Translations

The settings window is translated into 101 languages -- every language on the
Ubuntu-Hello list plus the three regional variants that predate it; the CLI's JSON is not, because
scripts parse it. Strings are marked with `_()` where they are displayed, and with
`translatable()` where they are written somewhere other than where they are shown -- the
runtime's validation failures, which the CLI prints verbatim and the window
translates on display.

After adding or changing a marked string:

```bash
scripts/build-translations.sh extract
```

That refreshes `po/wayland-vnc.pot` and merges it into all 101 catalogues, leaving the
new entries empty. Fill them in each `po/<lang>.po`, then:

```bash
scripts/build-translations.sh compile
```

`scripts/build-translations.sh check` is what the pipeline and CI run: it fails if the
template is stale against the marked strings, or if any catalogue does not compile.
The test suite additionally refuses a catalogue with a missing or blank message, or
one that drops a `%s` or `%(name)s` placeholder -- a dropped placeholder crashes the
window at render time, not at build time.

To see the window in a language without changing your session:

```bash
WAYLAND_VNC_LOCALEDIR=build/locale LANGUAGE=ro wayland-vnc-settings
```
