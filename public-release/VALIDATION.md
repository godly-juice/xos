# g37-public-beta.1 validation

Tested on November 1, 2026 (America/Chicago).

This record applies to `xos-g37-public.img`, 5,916,852,224 bytes, SHA-256:

```text
cc9825665583951554c85d2a2429f8ebe6fe0b212e9c15f16fcf5b87cf97dce3
```

## Completed checks

| Check | Result |
| --- | --- |
| Whole image, BIOS boot from virtual USB | Passed; native desktop-ready marker reached through the image's own bootloader. |
| Whole image, UEFI boot from virtual USB | Passed with OVMF; native desktop-ready marker reached. Secure Boot was not enabled for this test. |
| Try mode | Passed; fresh first-boot state, ordinary generic test-account creation, local model discovery, and no prior owner account. |
| Try mode disk preservation | Passed; a separate 4 GiB proof disk had identical full SHA-256 hashes before and after. |
| Fresh installation | Passed on a newly created 128 GiB virtual NVMe disk. Incorrect target-path and size confirmations were refused before the real install. |
| Installed system, disk alone | Passed using a direct kernel boot for API inspection, without the USB image attached. Fresh account creation, data/profile mounts, and local model discovery all passed. |
| Installed disk's own BIOS bootloader | Passed with the same installed disk attached as SATA and no USB image; native desktop-ready marker reached in about eight seconds. |
| Local AI after installation | Passed; Phi-4-mini generated a nonempty response through the local Ollama API. No external model service was contacted. |
| Image integrity after QA | Passed; the before/after image SHA-256 hashes match the release hash above. |

The VMs used QEMU/KVM, two virtual CPUs and 8 GiB RAM. Guest networking was restricted, with no external guest connections or host port forwards. All installation targets were new disposable virtual disk files; no physical disk or existing user disk was attached.

## Privacy and packaging

The public build was made separately from the original development image. The seed contains exactly five allowlisted standard model files; their hashes were checked against the model manifest. Personal Canon/memory databases and embeddings, preferences/persona state, private application/runtime data, prior profiles, saved authentication/network state, logs, and device enrollment were excluded or reset. The final scoped privacy audit had no unresolved findings. See the [privacy summary](https://github.com/godly-juice/xos/blob/main/public-release/privacy-summary.md).

The running Try environment independently confirmed that its seed has no Canon or XSCN members and that the prior owner account is absent. Installed first boot independently confirmed the prior owner account is absent. Developer-machine background work and private helper/failover services are disabled by default.

The four image parts have individual SHA-256 values in `IMAGE-MANIFEST.json` and `SHA256SUMS`. The Windows downloader verifies each part and the assembled image. Uploaded asset sizes and GitHub's server-side SHA-256 digests were checked against the local release files.

QA instrumentation was added only to extracted test initrds and the disposable installed disk. It is not in the released image. The source image was attached read-only, and its final hash remained unchanged.

## Limits

These are boot, installation, privacy, and service smoke tests. They do not certify the visual interface, every application, physical hardware compatibility, Secure Boot, or model performance. The installed-system API inspection used a direct kernel boot, followed by the separate successful BIOS/SATA bootloader test. The test VM's SeaBIOS did not read the NVMe controller, so installed NVMe/BIOS boot is not claimed; installed-disk UEFI boot was not separately tested. Live/recovery retains its temporary boot-console behavior; installed root login is locked. External services still require the user's own configuration and credentials.
