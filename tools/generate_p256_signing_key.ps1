param(
    [Parameter(Mandatory = $true)]
    [string]$Output,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Z][A-Z0-9_]+$')]
    [string]$EnvironmentVariableName
)

$ErrorActionPreference = 'Stop'
if ($env:OS -cne 'Windows_NT') {
    throw 'This signing-key generator requires Windows ACL support.'
}
$outputPath = [IO.Path]::GetFullPath($Output)
if ([IO.File]::Exists($outputPath)) {
    throw 'Refusing to overwrite an existing signing key.'
}

$parent = [IO.Path]::GetDirectoryName($outputPath)
if (-not [IO.Directory]::Exists($parent)) {
    throw 'The signing-key parent directory must already exist with restricted ACLs.'
}
$repositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$relativeToRepository = [IO.Path]::GetRelativePath($repositoryRoot, $outputPath)
if (-not [IO.Path]::IsPathRooted($relativeToRepository) -and $relativeToRepository -notlike '..\*') {
    throw 'Refusing to create signing material inside the source repository.'
}

$currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$currentName = $currentIdentity.Name
$currentSid = $currentIdentity.User.Value
$systemSid = 'S-1-5-18'
$parentAcl = Get-Acl -LiteralPath $parent
$unexpectedAllow = @(
    $parentAcl.Access | Where-Object {
        $_.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
        $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -notin @($currentSid, $systemSid)
    }
)
$currentAllowsWrite = @(
    $parentAcl.Access | Where-Object {
        $_.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
        $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -eq $currentSid -and
        ($_.FileSystemRights -band [Security.AccessControl.FileSystemRights]::Write) -ne 0
    }
).Count -gt 0
if (-not $parentAcl.AreAccessRulesProtected -or $unexpectedAllow.Count -ne 0 -or -not $currentAllowsWrite) {
    throw 'The signing-key parent directory ACL is not restricted to the current user and SYSTEM.'
}
$temporary = Join-Path $parent ('.' + [IO.Path]::GetFileName($outputPath) + '.' + [Guid]::NewGuid().ToString('N') + '.tmp')

$signer = [Security.Cryptography.ECDsa]::Create()
$signer.GenerateKey([Security.Cryptography.ECCurve+NamedCurves]::nistP256)
[byte[]]$privateKey = $null
try {
    $privateKey = $signer.ExportPkcs8PrivateKey()
    $privateKeyBase64 = [Convert]::ToBase64String($privateKey)
    $publicKeyBase64 = [Convert]::ToBase64String($signer.ExportSubjectPublicKeyInfo())
    $content = $EnvironmentVariableName + '=' + $privateKeyBase64 + [Environment]::NewLine
    [IO.File]::WriteAllText($temporary, $content, [Text.UTF8Encoding]::new($false))
    [IO.File]::Move($temporary, $outputPath, $false)
    & icacls.exe $outputPath /inheritance:r /grant:r "${currentName}:(F)" 'SYSTEM:(F)' | Out-Null
    if ($LASTEXITCODE -ne 0) {
        [IO.File]::Delete($outputPath)
        throw 'Could not protect the generated signing key with a private ACL.'
    }
    Write-Output $publicKeyBase64
} finally {
    if ([IO.File]::Exists($temporary)) {
        [IO.File]::Delete($temporary)
    }
    if ($null -ne $privateKey) {
        [Array]::Clear($privateKey, 0, $privateKey.Length)
    }
    $privateKeyBase64 = $null
    $content = $null
    $signer.Dispose()
}
