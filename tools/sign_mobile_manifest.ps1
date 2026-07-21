param(
    [Parameter(Mandatory = $true)]
    [string]$Manifest,

    [Parameter(Mandatory = $true)]
    [string]$Output
)

$ErrorActionPreference = 'Stop'
$encodedPrivateKey = $env:MOBILE_DATA_SIGNING_PRIVATE_KEY_BASE64
if ([string]::IsNullOrWhiteSpace($encodedPrivateKey)) {
    throw 'MOBILE_DATA_SIGNING_PRIVATE_KEY_BASE64 is required.'
}

$manifestPath = [IO.Path]::GetFullPath($Manifest)
$outputPath = [IO.Path]::GetFullPath($Output)
if (-not [IO.File]::Exists($manifestPath)) {
    throw "Manifest file does not exist: $manifestPath"
}

$privateKey = [Convert]::FromBase64String($encodedPrivateKey)
$signer = [Security.Cryptography.ECDsa]::Create()
try {
    $bytesRead = 0
    $signer.ImportPkcs8PrivateKey($privateKey, [ref]$bytesRead)
    if ($bytesRead -ne $privateKey.Length) {
        throw 'The mobile signing key contains trailing data.'
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
    [Array]::Clear($privateKey, 0, $privateKey.Length)
}
