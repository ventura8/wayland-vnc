# Security

Do not expose classic VNC to the public internet. VNC authentication alone does not
encrypt the session; legacy authentication only uses the first eight password
characters. Use a VPN for confidential Android access. The network default is this
machine only: the server binds `127.0.0.1:5900`. Turning on **Local Network Access**
in the settings app binds every interface, which reaches every device on every
network this machine is on (a shared Wi-Fi included). Nothing fences that further:
systemd applies `IPAddressAllow` only from a privileged manager, and a user manager
logs "unit configures an IP firewall, but not running as root" and attaches nothing,
so the unit carries no such claim. Do not forward the port through a router. A random
mode-600 password is generated on first start; the server never runs
unauthenticated.

Never commit credentials or private viewer binaries. Reports should contain only
sanitized synthetic-session evidence. Do not include framebuffer contents from a
personal desktop. Report security issues privately through the repository's GitHub
security reporting facility when enabled; do not publish exploit credentials.

No login-screen access, portal bypass, silent service replacement, or X11 server
fallback is permitted. Never run untrusted PRs on credential-bearing self-hosted
runners. Release qualification cannot be inferred from a successful TCP connection.
