# g37 public beta source snapshot

This is a focused source snapshot of the g37 add-ons and the shared legacy shell theme. It is not a complete operating-system build tree, a bootable image, or a tested installer. It contains the graphite/amber Operator Desk theme for the native window-control strip and hidden-window dock, together with matching legacy shell tokens.

## Source layout

- `src/g37/apply_g37.py`: the add-on coordinator.
- `src/g37/addons/apply_*.py`: individual add-on applicators.
- `src/g37/addons/{ball,console,winbar,worker}/`: implementations and example device profiles.
- `src/lumi/shell/layout.css`: the legacy installer/Home/first-run shell layout and theme.

The relative source paths are retained from the development workspace. Machine-specific portal defaults now use `https://portal.example.invalid`, a reserved invalid hostname. Configure a hub that you operate before enabling remote functionality. Existing loopback model defaults are retained. The Windows console activity directory defaults to the current user's local application-data directory; `LUMI_ACTIVITY_DIR` remains available as an override.

## Applicator gates and omitted material

No `.PASSED` markers are included. Consequently, the coordinator skips every add-on until each is independently reviewed and validated against a compatible disposable system tree and the release maintainer supplies the corresponding validation marker. An unchanged successful exit with all add-ons listed as skipped is not proof that the add-ons work.

Internal specs/reviews/dispatch tooling, private build-path tests, stick-update payloads, disk images, credentials, private state, personal data, model weights, and development history are not included. In particular, the stick-update applicator requires payloads that are absent here. Some add-ons depend on base-system modules, desktop assets, services, model software, and earlier-generation components that this snapshot does not provide.

The native UI requires Linux with GTK3, GtkLayerShell, and Sway; other add-ons need their compatible base-system dependencies. This public snapshot has passed Python and JSON parsing checks only. It has not been rebuilt, installed, booted, or validated on native hardware as part of this publication. Native visual checks for the new theme remain outstanding.

## Notices

Existing source headers are retained except for generalized personal/machine references. Copyright and license notices are preserved. This snapshot does not add or invent a software license.
