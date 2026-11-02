# g37 public release and privacy summary

This release adds a free bootable g37 beta alongside the published source snapshot. g38 remains planned work. The planned $1/month supporter offer is inactive.

The public image uses a models-only seed with the standard Phi-4-mini assets and their Microsoft MIT notice. It excludes personal memory and derived embeddings, private databases, preferences/persona state, private application/runtime data, and hosted accounts.

Cleanup removes prior user profiles, authentication/session material, saved network and pairing state, histories, logs, personal caches, enrollment, and old configuration backups. First boot creates a new account and machine identity. Installed root login is locked; the temporary live/recovery environment retains its boot-console behavior.

Developer-machine work, remote enrollment, private helper/failover connections, and legacy private-runtime startup are disabled by default. Generic runtime components and their notices are retained. The approved desktop theme is included.

The release provides image checksums, a manifest, retained notices, a package inventory, and a source archive. See the release's `VALIDATION.md` for completed checks and known limits; this summary makes no additional hardware or app-compatibility claim. No blanket license is added for the complete distribution.
