# Test instructions

Read ../docs/development.md. Unit fakes may model external boundaries; E2E must use
real compositors, real portals and actual RealVNC Android and desktop applications.
Do not turn missing prerequisites into skips or create fabricated passing evidence.
Use only synthetic framebuffers and disposable credentials. Save sanitized failure
artifacts with bounded retention. Never attach to a personal desktop or a personal
AVD. The only Android device permitted is the isolated, project-owned AVD created
by `scripts/android-lab.py`, which refuses to overwrite an existing AVD and never
reads personal Android configuration.
