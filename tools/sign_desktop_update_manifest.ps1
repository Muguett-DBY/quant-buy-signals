param(
    [Parameter(Mandatory = $true)]
    [string]$Manifest,

    [Parameter(Mandatory = $true)]
    [string]$Output,

    [string]$PrivateKeyPath
)

$ErrorActionPreference = 'Stop'
$expectedPublicKey = 'MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEbzdnA3j6aObU/Z0HlTTC+PziXVm4hZ/pqSQrUWeC2mm/INuge2qyT67vWxpTC7yPDzFdHOBenDnQ8lMEilPKDw=='

function ConvertTo-PrivateKeyBytes([byte[]]$raw) {
    try {
        $text = [Text.Encoding]::UTF8.GetString($raw)
        $property = [regex]::Match(
            $text,
            '(?m)^\s*DS_DCF_DESKTOP_SIGNING_PRIVATE_KEY_BASE64\s*=\s*(?<key>[A-Za-z0-9+/=]+)\s*$'
        )
        if ($property.Success) {
            return [Convert]::FromBase64String($property.Groups['key'].Value)
        }
        $pem = [regex]::Match(
            $text,
            '(?s)-----BEGIN PRIVATE KEY-----\s*(?<key>[A-Za-z0-9+/\s=]+?)\s*-----END PRIVATE KEY-----'
        )
        if ($pem.Success) {
            return [Convert]::FromBase64String(($pem.Groups['key'].Value -replace '\s', ''))
        }
    } catch [Text.DecoderFallbackException] {
        # A binary PKCS#8 DER file is accepted below.
    }
    return $raw
}

$manifestPath = [IO.Path]::GetFullPath($Manifest)
$outputPath = [IO.Path]::GetFullPath($Output)
if (-not [IO.File]::Exists($manifestPath)) {
    throw 'Canonical desktop update manifest does not exist.'
}

[byte[]]$privateKey = $null
if (-not [string]::IsNullOrWhiteSpace($PrivateKeyPath)) {
    $resolvedPrivateKeyPath = [IO.Path]::GetFullPath($PrivateKeyPath)
    if (-not [IO.File]::Exists($resolvedPrivateKeyPath)) {
        throw 'Desktop update signing private key file does not exist.'
    }
    $privateKey = ConvertTo-PrivateKeyBytes ([IO.File]::ReadAllBytes($resolvedPrivateKeyPath))
} else {
    $encodedPrivateKey = $env:DS_DCF_DESKTOP_SIGNING_PRIVATE_KEY_BASE64
    if ([string]::IsNullOrWhiteSpace($encodedPrivateKey)) {
        throw 'DS_DCF_DESKTOP_SIGNING_PRIVATE_KEY_BASE64 is required.'
    }
    $privateKey = [Convert]::FromBase64String($encodedPrivateKey)
}

$signer = [Security.Cryptography.ECDsa]::Create()
try {
    $bytesRead = 0
    $signer.ImportPkcs8PrivateKey($privateKey, [ref]$bytesRead)
    if ($bytesRead -ne $privateKey.Length) {
        throw 'The desktop signing private key contains trailing data.'
    }
    $actualPublicKey = [Convert]::ToBase64String($signer.ExportSubjectPublicKeyInfo())
    if ($actualPublicKey -cne $expectedPublicKey) {
        throw 'The desktop signing private key does not match the public key pinned in the application.'
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
