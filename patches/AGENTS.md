# Native patches

Read ../docs/development.md. Never patch the live troubleshooting source tree.
Separate correctness fixes from empirical compatibility profiles. Every patch must
apply cleanly to a SHA-256-verified source archive and carry an upstream/license
note. No unconditional encoding override: honor advertised capabilities. Test
client disconnection, partial FD duplication failure, and buffer bounds under
sanitizers. Passing helper tests alone does not qualify the integrated daemon.
