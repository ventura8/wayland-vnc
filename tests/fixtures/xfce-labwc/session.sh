#!/bin/sh
# labwc's --session command for the Xfce fixture: switch the spare headless output
# off, THEN start xfce4-session. The order matters. wlroots 0.19 hands a client the
# head's "virtual mode" only if the head is enabled when the client binds, and once
# any client holds one it never sends it to clients that bound while the head was
# disabled -- so enabling the spare later asserts inside labwc
# (wlr_output_management_v1.c: head_send_state, `found`) whenever a session client
# such as xfsettingsd, which binds wlr-output-management for good, saw the output
# enabled first. Started in sequence, no session client ever does.
set -eu
wlr-randr --output "${FIXTURE_SPARE_OUTPUT:-HEADLESS-2}" --off
exec xfce4-session
