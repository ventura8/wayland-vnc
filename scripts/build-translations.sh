#!/usr/bin/env bash
# Extract the translatable strings and compile every catalogue.
#
#   build-translations.sh extract   refresh po/wayland-vnc.pot and merge it into each .po
#   build-translations.sh compile   compile every .po into build/locale (the default)
#   build-translations.sh check     fail if the template is stale or a catalogue is broken
#
# The settings window is what a person reads, so it is what gets translated; the CLI's
# JSON stays in English because scripts parse it.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

domain=wayland-vnc
potfile=po/$domain.pot
outdir=${WAYLAND_VNC_LOCALEDIR:-build/locale}
sources=(src/wayland_vnc/settings.py src/wayland_vnc/settings_app.py
  src/wayland_vnc/settings_dialogs.py src/wayland_vnc/runtime.py
  src/wayland_vnc/probe.py)

extract_to() {
  local template=$1
  # --add-location=file keeps the source file a string came from, but not the line
  # number: line numbers move whenever anything above them is edited, which made the
  # staleness check below fail on changes that added no message at all.
  xgettext --language=Python --keyword=_ --keyword=translatable --from-code=UTF-8 \
    --package-name="$domain" --package-version="$(cat VERSION 2>/dev/null || echo 1.0.0)" \
    --msgid-bugs-address=https://github.com/ventura8/wayland-vnc/issues \
    --add-comments=Translators --no-wrap --add-location=file --output="$template" \
    "${sources[@]}"
  # xgettext leaves a CHARSET placeholder and stamps a build date into the header. Fix
  # the charset before anything else reads the file (msgcat warns about the
  # placeholder), and drop the date so the template does not differ from itself on
  # every run, which would defeat the staleness check below.
  sed -i -e 's/charset=CHARSET/charset=UTF-8/' -e '/^"POT-Creation-Date:/d' "$template"
  # xgettext's own --sort-output is deprecated; msgcat is the supported way to get a
  # stable order, which keeps the template's diff readable between runs.
  msgcat --sort-output --no-wrap --output-file="$template" "$template"
}

case "${1:-compile}" in
extract)
  mkdir -p po
  extract_to "$potfile"
  for po in po/*.po; do
    [[ -e "$po" ]] || continue
    # No --sort-output: it is deprecated, and msgmerge already follows the
    # template's order, which msgcat has already sorted.
    msgmerge --quiet --no-wrap --update --backup=none "$po" "$potfile"
  done
  echo "extracted $(grep -c '^msgid "' "$potfile") messages to $potfile"
  ;;
compile)
  mkdir -p "$outdir"
  count=0
  for po in po/*.po; do
    [[ -e "$po" ]] || continue
    lang=$(basename "$po" .po)
    mkdir -p "$outdir/$lang/LC_MESSAGES"
    # Compiled by Python, not msgfmt: packaging runs in containers that have Python
    # but not the gettext tools, and building what ships through one code path means
    # the tests cover exactly what users get. msgfmt still validates in `check`.
    python3 scripts/compile_catalogue.py "$po" \
      "$outdir/$lang/LC_MESSAGES/$domain.mo" >/dev/null
    count=$((count + 1))
  done
  echo "compiled $count catalogues into $outdir"
  ;;
check)
  tmp=$(mktemp)
  trap 'rm -f "$tmp"' EXIT
  extract_to "$tmp"
  if ! diff -q "$potfile" "$tmp" >/dev/null; then
    echo "po/$domain.pot is stale; run: scripts/build-translations.sh extract" >&2
    diff -u "$potfile" "$tmp" | head -40 >&2
    exit 1
  fi
  status=0
  for po in po/*.po; do
    [[ -e "$po" ]] || continue
    msgfmt --check --check-format --output-file=/dev/null "$po" || status=1
  done
  exit "$status"
  ;;
*)
  echo "usage: build-translations.sh [extract|compile|check]" >&2
  exit 2
  ;;
esac
