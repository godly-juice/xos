# g37 source beta

## Scope

The repository includes g37 add-on programs, their applicators, generic configuration examples and the current UI theme. The underlying xOS system tree, kernel, packages, model weights and full image builder are not included. This snapshot cannot independently produce or install the complete operating system.

The native desktop uses GTK 3, GtkLayerShell and Sway. The legacy shell CSS is a separate surface. Python source compilation and publication checks do not establish that every desktop behavior works on real hardware.

## Add-on gate

`src/g37/apply_g37.py` only runs add-ons with a matching `addons/<name>.PASSED` marker. Internal historical markers are intentionally excluded from this public snapshot: they are not transferable proof that the current source passes on another system. Without markers the dispatcher skips those add-ons. Do not add markers merely to bypass validation.

The applicators require an existing compatible xOS root. Review an individual applicator before using it, and use a disposable copy or VM. They are not a general installation command. This repository does not contain a public disk flasher.

## Checks and known limits

- Current published Python files are checked for syntax. Publication review excludes private data and replaces machine-specific runtime defaults where documented.
- The current window-control and shell theme changes passed source-level checks. Native visual QA has not run on this Windows publication host.
- Historical local g37 boot/update testing applies to the earlier local image, not automatically to the later theme snapshot or any future clean public image.
- g38 is a plan. No g38 bootable build is included.

See [the source-publication notes](docs/g37-source.md) for exact exclusions and source changes.

## Bootable beta milestone

Before an image can be published, build it from a clean generic profile without personal Canon/memory, credentials, pairing state or private service configuration; verify component redistribution requirements; then test boot, live try mode and installation/update on disposable targets. Publish checksums for that exact clean artifact.

No personal image is offered as a public download in the meantime.

## Reporting a bug

Open an issue with the component, revision, what you tried, expected behavior and what happened. Include your OS/runtime and a small redacted log or screenshot when useful. Do not attach personal memory databases, full account profiles, passwords or keys.
