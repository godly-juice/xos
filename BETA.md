# g37 public beta

The [g37-public-beta.1 release](https://github.com/godly-juice/xos/releases/tag/g37-public-beta.1) provides a free experimental bootable image alongside the source snapshot. g38 remains a [plan](docs/g38-plan.md), not a released build.

## Download and assemble

**Windows:** download `Download-xOS.ps1` from the release, inspect it, and run:

```powershell
./Download-xOS.ps1 -Directory ./xos-beta
```

The script verifies each part, joins them, and verifies the complete `xos-g37-public.img`. It creates files only in its output directory. It does not select a USB drive, flash media, or install the operating system.

**Manual download:** obtain every `xos-g37-public.img.partNN` file, `IMAGE-MANIFEST.json`, and `SHA256SUMS` from the same release. Join the parts in numerical order. On Linux:

```sh
sha256sum --check SHA256SUMS --ignore-missing
cat xos-g37-public.img.part[0-9][0-9] > xos-g37-public.img
sha256sum --check SHA256SUMS
```

Use the assembled `.img`, never an individual part. Allow space for both the parts and the completed image; `IMAGE-MANIFEST.json` gives the exact sizes.

## Try first

Use a disposable VM or a spare USB drive of at least 16 GB. Flashing erases the selected USB drive. Choose **Try xOS** before considering installation; installing can erase its selected target disk. Keep backups and use a spare disk for beta testing.

The image targets x86-64 PCs with at least 8 GB RAM. The local model also needs available memory; Try mode may skip it on smaller machines. Create your own account at first boot. The live/recovery environment is a temporary testing environment, not a secured shared workstation.

## Privacy and service scope

The public build uses a fresh identity and a models-only seed. Personal memory, databases and embeddings, private app/runtime data, saved account and network state, credentials, and existing device enrollment are excluded. Installed root login is locked. Automatic developer-machine work and private failover connections are disabled.

The standard Phi-4-mini assets include their Microsoft MIT notice. Private XSCN runtime/data and hosted accounts are not included. Features needing external services or credentials are not provisioned by this image.

## Source and validation

The repository contains g37 add-ons, generic configuration, and UI source. It is not a complete OS bootstrap or standalone builder. Add-on applicators require a compatible root; historical `.PASSED` markers are intentionally omitted, so the coordinator skips unvalidated add-ons. Do not create markers just to bypass checks. See [source notes](docs/g37-source.md).

The release includes a source archive, package inventory, retained notices, and `VALIDATION.md`. That validation record describes the exact artifact's completed checks and known limits. Earlier development-image results do not automatically apply to this public image, and VM results do not establish compatibility with every physical PC or every app.

Beta access is free. The planned $1/month Supporter option is inactive and has no checkout. See [PRICING.md](PRICING.md).

For feedback, open an [issue](https://github.com/godly-juice/xos/issues) with the release/build ID, expected behavior, actual behavior, and redacted evidence.
