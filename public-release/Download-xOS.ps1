param([string]$Directory = (Join-Path (Get-Location) 'xos-g37-public-beta'))
$ErrorActionPreference = 'Stop'
$releaseBase = 'https://github.com/godly-juice/xos/releases/download/g37-public-beta.1'
$targetDirectory = [IO.Path]::GetFullPath($Directory)
[IO.Directory]::CreateDirectory($targetDirectory) | Out-Null
$manifest = Invoke-RestMethod -Uri "$releaseBase/IMAGE-MANIFEST.json"
if ($manifest.schema -ne 1 -or $manifest.image.name -ne 'xos-g37-public.img' -or $manifest.parts.Count -lt 1) { throw 'Unexpected release manifest.' }
if ($manifest.image.sha256 -notmatch '^[a-f0-9]{64}$') { throw 'Invalid image checksum.' }
$imagePath = Join-Path $targetDirectory $manifest.image.name
if (Test-Path -LiteralPath $imagePath) {
    if ((Get-FileHash -LiteralPath $imagePath -Algorithm SHA256).Hash.ToLowerInvariant() -eq $manifest.image.sha256) { Write-Host "Already downloaded and verified: $imagePath"; exit 0 }
    throw 'An existing image has a different checksum. Choose another output directory.'
}
$partPaths = @()
foreach ($part in $manifest.parts) {
    if ($part.name -notmatch '^xos-g37-public\.img\.part[0-9]{2}$' -or $part.sha256 -notmatch '^[a-f0-9]{64}$') { throw 'Invalid part metadata.' }
    $partPath = Join-Path $targetDirectory $part.name
    $valid = (Test-Path -LiteralPath $partPath) -and ((Get-Item -LiteralPath $partPath).Length -eq $part.bytes)
    if ($valid) { $valid = ((Get-FileHash -LiteralPath $partPath -Algorithm SHA256).Hash.ToLowerInvariant() -eq $part.sha256) }
    if (-not $valid) {
        if (Test-Path -LiteralPath $partPath) { throw "Existing part differs from release: $partPath. Choose another output directory." }
        Write-Host "Downloading $($part.name)..."
        $partialPath = "$partPath.download"
        Invoke-WebRequest -UseBasicParsing -Uri "$releaseBase/$($part.name)" -OutFile $partialPath
        if ((Get-Item -LiteralPath $partialPath).Length -ne $part.bytes -or (Get-FileHash -LiteralPath $partialPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $part.sha256) { throw "Checksum failed: $($part.name)" }
        Move-Item -LiteralPath $partialPath -Destination $partPath
    }
    $partPaths += $partPath
}
$assemblyPath = "$imagePath.assembling"
$destination = [IO.File]::Open($assemblyPath, [IO.FileMode]::Create, [IO.FileAccess]::Write)
try {
    foreach ($partPath in $partPaths) {
        $source = [IO.File]::OpenRead($partPath)
        try { $source.CopyTo($destination, 8388608) } finally { $source.Dispose() }
    }
} finally { $destination.Dispose() }
if ((Get-Item -LiteralPath $assemblyPath).Length -ne $manifest.image.bytes -or (Get-FileHash -LiteralPath $assemblyPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $manifest.image.sha256) { throw 'Final image checksum failed.' }
Move-Item -LiteralPath $assemblyPath -Destination $imagePath
Write-Host "Downloaded and SHA-256 verified: $imagePath"
Write-Host 'This script only downloads files. It has not written to a USB drive or changed your computer.'
Write-Host 'Read BOOTABLE-BETA.md in the release before booting or installing.'
