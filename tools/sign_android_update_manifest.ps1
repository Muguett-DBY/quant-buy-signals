param(
    [Parameter(Mandatory = $true)]
    [string]$Manifest,

    [Parameter(Mandatory = $true)]
    [string]$Output
)

$ErrorActionPreference = 'Stop'
$expectedPublicKey = 'MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEjic3+c4snSCoVhcipasA9t3ppCwvRO5u88dg/M1oul+Y3Wp0BwR/Z9bq9ywZK3NgDn7SH3pluAU3MOdQqcVoIA=='
$encodedPrivateKey = $env:DS_DCF_ANDROID_UPDATE_SIGNING_PRIVATE_KEY_BASE64
if ([string]::IsNullOrWhiteSpace($encodedPrivateKey)) {
    throw 'DS_DCF_ANDROID_UPDATE_SIGNING_PRIVATE_KEY_BASE64 is required.'
}

$manifestPath = [IO.Path]::GetFullPath($Manifest)
$outputPath = [IO.Path]::GetFullPath($Output)
if (-not [IO.File]::Exists($manifestPath)) {
    throw 'Canonical Android update manifest does not exist.'
}
if ([StringComparer]::OrdinalIgnoreCase.Equals($manifestPath, $outputPath)) {
    throw 'The detached signature output must not overwrite the Android update manifest.'
}

[byte[]]$privateKey = [Convert]::FromBase64String($encodedPrivateKey)
$signer = [Security.Cryptography.ECDsa]::Create()
try {
    $bytesRead = 0
    $signer.ImportPkcs8PrivateKey($privateKey, [ref]$bytesRead)
    if ($bytesRead -ne $privateKey.Length) {
        throw 'The Android update signing private key contains trailing data.'
    }
    $actualPublicKey = [Convert]::ToBase64String($signer.ExportSubjectPublicKeyInfo())
    if ($actualPublicKey -cne $expectedPublicKey) {
        throw 'The Android update signing private key does not match the public key pinned in the application.'
    }
    $signature = $signer.SignData(
        [IO.File]::ReadAllBytes($manifestPath),
        [Security.Cryptography.HashAlgorithmName]::SHA256,
        [Security.Cryptography.DSASignatureFormat]::Rfc3279DerSequence
    )
    $parent = [IO.Path]::GetDirectoryName($outputPath)
    [IO.Directory]::CreateDirectory($parent) | Out-Null
    $temporary = Join-Path $parent ('.' + [IO.Path]::GetFileName($outputPath) + '.' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::WriteAllBytes($temporary, $signature)
        [IO.File]::Move($temporary, $outputPath, $true)
    } finally {
        if ([IO.File]::Exists($temporary)) {
            [IO.File]::Delete($temporary)
        }
    }
} finally {
    $signer.Dispose()
    if ($null -ne $privateKey) {
        [Array]::Clear($privateKey, 0, $privateKey.Length)
    }
}
