# TigerVNC patches

None yet. `w0vncserver` 1.16.2 is built unmodified from the checksum-verified
upstream archive listed in `sources.json`; only the `w0vncserver`,
`w0vncserver-forget` and `vncpasswd` targets are installed (`vncpasswd` obfuscates
the viewer password for generated `.vnc` connection files). The X11 servers are
never installed.
