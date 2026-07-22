[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [ValidateSet('schedule', 'workflow_dispatch')]
  [string]$EventName,

  [string]$CalendarPath = (Join-Path $PSScriptRoot 'china_a_share_trading_calendar.json'),
  [string]$AndroidSourcePath = (
    Join-Path (Split-Path -Parent $PSScriptRoot) 'android/app/src/main/java/com/muguett/dsdcf/MarketRepository.java'
  ),
  [string]$ManifestUrl = 'https://muguett-dby.github.io/quant-buy-signals/mobile-data/manifest.json',
  [string]$ReleaseBaseUrl = (
    'https://github.com/Muguett-DBY/quant-buy-signals/releases/download/mobile-market-data'
  ),
  [string]$ArchiveBaseUrl = (
    'https://raw.githubusercontent.com/Muguett-DBY/quant-buy-signals/mobile-data/latest'
  ),
  [string]$ManifestPath,
  [string]$ReleaseDirectory,
  [string]$ArchiveDirectory,
  [string]$NowUtc,
  [string]$ExpectedSourceCommit = $env:GITHUB_SHA,
  [ValidateRange(1, 6500)]
  [int]$MinimumCompanyCount = 4500,
  [ValidateRange(1, 6500)]
  [int]$MaximumCompanyCount = 6500,
  [string]$OutputPath = $env:GITHUB_OUTPUT
)

$ErrorActionPreference = 'Stop'
$script:MaximumManifestBytes = 1000000
$script:MaximumPayloadBytes = 8000000
$script:MaximumUncompressedPayloadBytes = 16000000
if ($MaximumCompanyCount -lt $MinimumCompanyCount) {
  throw 'MaximumCompanyCount cannot be lower than MinimumCompanyCount.'
}
$script:MinimumCompanyCount = $MinimumCompanyCount
$script:MaximumCompanyCount = $MaximumCompanyCount
$script:MaximumFutureClockSkew = [TimeSpan]::FromMinutes(10)
$script:PostCloseReadyTime = [TimeSpan]::FromMinutes(975)
$script:ValidTypeStatuses = @(
  'triggered',
  'conditional',
  'observe',
  'insufficient_evidence',
  'vetoed',
  'blocked',
  'not_triggered',
  'not_applicable'
)
$script:ShanghaiOffset = [TimeSpan]::FromHours(8)
$script:ShanghaiZone = [TimeZoneInfo]::CreateCustomTimeZone(
  'DS_DCF_Asia_Shanghai',
  $script:ShanghaiOffset,
  'Asia/Shanghai',
  'Asia/Shanghai'
)

function Write-WorkflowDecision([bool]$ShouldRun, [string]$Reason) {
  $value = if ($ShouldRun) { 'true' } else { 'false' }
  Write-Host "Mobile market workflow decision: should_run=$value; reason=$Reason"
  if (-not [string]::IsNullOrWhiteSpace($OutputPath)) {
    "should_run=$value" | Out-File -LiteralPath $OutputPath -Encoding utf8 -Append
    "reason=$Reason" | Out-File -LiteralPath $OutputPath -Encoding utf8 -Append
  }
}

function Get-ShanghaiNow {
  if ([string]::IsNullOrWhiteSpace($NowUtc)) {
    $instant = [DateTimeOffset]::UtcNow
  } else {
    $instant = [DateTimeOffset]::ParseExact(
      $NowUtc,
      'o',
      [Globalization.CultureInfo]::InvariantCulture,
      [Globalization.DateTimeStyles]::RoundtripKind
    )
  }
  return [TimeZoneInfo]::ConvertTime($instant, $script:ShanghaiZone)
}

function Assert-UniqueJsonProperties([Text.Json.JsonElement]$Element, [string]$Path = '$') {
  if ($Element.ValueKind -eq [Text.Json.JsonValueKind]::Object) {
    $names = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    foreach ($property in $Element.EnumerateObject()) {
      if (-not $names.Add($property.Name)) {
        throw "JSON contains duplicate property $Path.$($property.Name)."
      }
      Assert-UniqueJsonProperties $property.Value "$Path.$($property.Name)"
    }
  } elseif ($Element.ValueKind -eq [Text.Json.JsonValueKind]::Array) {
    $index = 0
    foreach ($item in $Element.EnumerateArray()) {
      Assert-UniqueJsonProperties $item "$Path[$index]"
      $index++
    }
  }
}

function ConvertFrom-StrictJsonBytes([byte[]]$Bytes, [string]$Label) {
  if ($null -eq $Bytes -or $Bytes.Length -le 0) {
    throw "$Label is empty."
  }
  $reader = [Text.UTF8Encoding]::new($false, $true)
  $text = $reader.GetString($Bytes)
  $document = [Text.Json.JsonDocument]::Parse($text)
  try {
    if ($document.RootElement.ValueKind -ne [Text.Json.JsonValueKind]::Object) {
      throw "$Label must be a JSON object."
    }
    Assert-UniqueJsonProperties $document.RootElement
  } finally {
    $document.Dispose()
  }
  try {
    return $text | ConvertFrom-Json
  } catch {
    throw "$Label is not valid JSON: $($_.Exception.Message)"
  }
}

function Read-StrictJsonFile([string]$Path, [long]$MaximumBytes, [string]$Label) {
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
    throw "$Label is unavailable."
  }
  $item = Get-Item -LiteralPath $Path
  if ($item.Length -le 0 -or $item.Length -gt $MaximumBytes) {
    throw "$Label has an invalid byte length."
  }
  $bytes = [IO.File]::ReadAllBytes($Path)
  return ConvertFrom-StrictJsonBytes $bytes $Label
}

function Get-CalendarDecision([DateTimeOffset]$ShanghaiNow) {
  $calendar = Read-StrictJsonFile $CalendarPath $script:MaximumManifestBytes 'Trading calendar'
  if (
    [int]$calendar.schema_version -ne 1 -or
    [string]$calendar.market -cne 'Shanghai and Shenzhen A-share markets' -or
    [string]$calendar.timezone -cne 'Asia/Shanghai'
  ) {
    throw 'Trading calendar identity is invalid.'
  }
  if ($ShanghaiNow.DayOfWeek -in @([DayOfWeek]::Saturday, [DayOfWeek]::Sunday)) {
    return @{ closed = $true; reason = 'market_closed_weekend' }
  }
  $year = [string]$ShanghaiNow.Year
  $entry = $calendar.years.PSObject.Properties[$year].Value
  if ($null -eq $entry) {
    # Unknown years must never be silently classified as holidays. Running the
    # fail-closed publisher preserves freshness until the official calendar is
    # pinned in this repository.
    return @{ closed = $false; reason = 'calendar_year_unavailable' }
  }
  $sources = @($entry.sources)
  $exchangeNames = @($sources.exchange | Sort-Object -Unique) -join ','
  if (
    $sources.Count -lt 2 -or
    $exchangeNames -cne 'SSE,SZSE' -or
    @($sources | Where-Object { [string]$_.url -notmatch '^https://www\.(?:sse\.com\.cn|szse\.cn)/' }).Count -ne 0
  ) {
    throw "Trading calendar $year does not retain both official exchange sources."
  }
  $today = $ShanghaiNow.Date
  foreach ($period in @($entry.closure_periods)) {
    $start = [DateTime]::ParseExact(
      [string]$period.start,
      'yyyy-MM-dd',
      [Globalization.CultureInfo]::InvariantCulture
    )
    $end = [DateTime]::ParseExact(
      [string]$period.end,
      'yyyy-MM-dd',
      [Globalization.CultureInfo]::InvariantCulture
    )
    if ($end -lt $start -or $start.Year -ne $ShanghaiNow.Year -or $end.Year -ne $ShanghaiNow.Year) {
      throw "Trading calendar $year contains an invalid closure period."
    }
    if ($today -ge $start -and $today -le $end) {
      return @{ closed = $true; reason = 'market_closed_exchange_notice' }
    }
  }
  return @{ closed = $false; reason = 'market_open' }
}

function Copy-RemoteFile([string]$Uri, [string]$Destination, [long]$MaximumBytes) {
  $lastFailure = $null
  for ($attempt = 1; $attempt -le 3; $attempt++) {
    try {
      Remove-Item -LiteralPath $Destination -Force -ErrorAction SilentlyContinue
      $response = Invoke-WebRequest `
        -Uri $Uri `
        -OutFile $Destination `
        -MaximumRedirection 10 `
        -TimeoutSec 45 `
        -PassThru
      if ([int]$response.StatusCode -ne 200) {
        throw "Remote asset returned HTTP $([int]$response.StatusCode)."
      }
      $item = Get-Item -LiteralPath $Destination
      if ($item.Length -le 0 -or $item.Length -gt $MaximumBytes) {
        throw 'Remote asset has an invalid byte length.'
      }
      return
    } catch {
      $lastFailure = $_
      if ($attempt -lt 3) {
        Start-Sleep -Seconds (2 * $attempt)
      }
    }
  }
  throw "Remote asset remained unavailable after three attempts: $($lastFailure.Exception.Message)"
}

function Copy-GenerationFile([string]$Name, [string]$Destination, [long]$MaximumBytes) {
  if (-not [string]::IsNullOrWhiteSpace($ReleaseDirectory)) {
    $source = Join-Path $ReleaseDirectory $Name
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
      throw "Published generation asset $Name is unavailable."
    }
    Copy-Item -LiteralPath $source -Destination $Destination
    $item = Get-Item -LiteralPath $Destination
    if ($item.Length -le 0 -or $item.Length -gt $MaximumBytes) {
      throw "Published generation asset $Name has an invalid byte length."
    }
    return
  }
  Copy-RemoteFile "$($ReleaseBaseUrl.TrimEnd('/'))/$Name" $Destination $MaximumBytes
}

function Copy-ArchiveFile([string]$Name, [string]$Destination, [long]$MaximumBytes) {
  if (-not [string]::IsNullOrWhiteSpace($ArchiveDirectory)) {
    $source = Join-Path $ArchiveDirectory $Name
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
      throw "Archived generation file $Name is unavailable."
    }
    Copy-Item -LiteralPath $source -Destination $Destination
    $item = Get-Item -LiteralPath $Destination
    if ($item.Length -le 0 -or $item.Length -gt $MaximumBytes) {
      throw "Archived generation file $Name has an invalid byte length."
    }
    return
  }
  $nonce = [Uri]::EscapeDataString([string][DateTimeOffset]::UtcNow.ToUnixTimeSeconds())
  Copy-RemoteFile "$($ArchiveBaseUrl.TrimEnd('/'))/${Name}?workflow_guard=$nonce" $Destination $MaximumBytes
}

function Get-RequiredAssetMetadata([object]$Manifest, [string]$Property, [string]$ExpectedName) {
  $metadata = $Manifest.$Property
  if ($null -eq $metadata -or [string]$metadata.filename -cne $ExpectedName) {
    throw "Published $Property metadata is invalid."
  }
  $size = 0L
  if (-not [long]::TryParse([string]$metadata.size, [ref]$size) -or $size -le 0 -or $size -gt $script:MaximumPayloadBytes) {
    throw "Published $Property size is invalid."
  }
  $sha256 = [string]$metadata.sha256
  if ($sha256 -cnotmatch '^[0-9a-f]{64}$') {
    throw "Published $Property SHA-256 is invalid."
  }
  return @{ size = $size; sha256 = $sha256 }
}

function Get-RequiredProperty([object]$Value, [string]$Name, [string]$Label) {
  if ($null -eq $Value) {
    throw "$Label is missing."
  }
  $property = $Value.PSObject.Properties[$Name]
  if ($null -eq $property) {
    throw "$Label omits $Name."
  }
  return $property.Value
}

function ConvertTo-RequiredInteger([object]$Value, [string]$Label) {
  if ($null -eq $Value -or $Value -is [bool] -or $Value -isnot [ValueType]) {
    throw "$Label must be an integer."
  }
  try {
    $number = [double]$Value
  } catch {
    throw "$Label must be an integer."
  }
  if (-not [double]::IsFinite($number) -or [Math]::Floor($number) -ne $number) {
    throw "$Label must be an integer."
  }
  return [long]$number
}

function ConvertTo-RequiredFiniteNumber([object]$Value, [string]$Label) {
  if ($null -eq $Value -or $Value -is [bool] -or ($Value -isnot [ValueType])) {
    throw "$Label must be a finite number."
  }
  try {
    $number = [double]$Value
  } catch {
    throw "$Label must be a finite number."
  }
  if (-not [double]::IsFinite($number)) {
    throw "$Label must be a finite number."
  }
  return $number
}

function Expand-StrictGzipJson([string]$Path, [string]$Label) {
  $source = [IO.File]::OpenRead($Path)
  $gzip = $null
  $output = [IO.MemoryStream]::new()
  try {
    $gzip = [IO.Compression.GzipStream]::new($source, [IO.Compression.CompressionMode]::Decompress, $false)
    $buffer = [byte[]]::new(65536)
    while (($read = $gzip.Read($buffer, 0, $buffer.Length)) -gt 0) {
      if ($output.Length + $read -gt $script:MaximumUncompressedPayloadBytes) {
        throw "$Label exceeds the Android uncompressed byte limit."
      }
      $output.Write($buffer, 0, $read)
    }
    return ConvertFrom-StrictJsonBytes $output.ToArray() $Label
  } catch {
    throw "$Label is not a valid bounded gzip JSON payload: $($_.Exception.Message)"
  } finally {
    if ($null -ne $gzip) {
      $gzip.Dispose()
    } else {
      $source.Dispose()
    }
    $output.Dispose()
  }
}

function Assert-AnalysisQuality([object]$Snapshot, [long]$CompanyCount, [string]$Label) {
  $quality = Get-RequiredProperty $Snapshot 'analysis_quality' "$Label analysis quality"
  if ($null -eq $quality -or (Get-RequiredProperty $quality 'ok' "$Label analysis quality") -ne $true) {
    throw "$Label did not retain a passing analysis quality gate."
  }
  foreach ($field in @('expected_companies', 'score_raw_rows', 'score_rows')) {
    if ((ConvertTo-RequiredInteger (Get-RequiredProperty $quality $field "$Label analysis quality") "$Label $field") -ne $CompanyCount) {
      throw "$Label analysis quality $field differs from the company count."
    }
  }
  if ((ConvertTo-RequiredInteger (Get-RequiredProperty $quality 'pipeline_issues' "$Label analysis quality") "$Label pipeline_issues") -ne 0) {
    throw "$Label analysis quality contains pipeline issues."
  }
  $coverage = ConvertTo-RequiredFiniteNumber (
    Get-RequiredProperty $quality 'score_coverage' "$Label analysis quality"
  ) "$Label score_coverage"
  if ($coverage -lt 0.99 -or $coverage -gt 1.0) {
    throw "$Label analysis score coverage is outside the Android acceptance range."
  }
}

function Assert-SharedSnapshotFields([object]$Manifest, [object]$Payload, [string]$Label) {
  if ((ConvertTo-RequiredInteger (Get-RequiredProperty $Payload 'schema_version' $Label) "$Label schema_version") -ne 1) {
    throw "$Label schema version is unsupported by the Android client."
  }
  if ([string](Get-RequiredProperty $Payload 'product' $Label) -cne 'DS_DCF') {
    throw "$Label product identity is invalid."
  }
  foreach ($field in @('market_as_of', 'data_timestamp_utc')) {
    if ([string](Get-RequiredProperty $Payload $field $Label) -cne [string](Get-RequiredProperty $Manifest $field 'Published manifest')) {
      throw "$Label $field differs from the published manifest."
    }
  }
  $payloadProvenance = Get-RequiredProperty $Payload 'provenance' "$Label provenance"
  if ([string](Get-RequiredProperty $payloadProvenance 'source_commit' "$Label provenance") -cne $ExpectedSourceCommit) {
    throw "$Label source commit differs from the running main revision."
  }
}

function Assert-StringTypeList([object]$Company, [string]$Property, [string]$Code) {
  if ($null -eq $Company) {
    throw "Company $Code is missing."
  }
  $propertyValue = $Company.PSObject.Properties[$Property]
  if ($null -eq $propertyValue) {
    throw "Company $Code omits $Property."
  }
  # Read the PSPropertyInfo value directly. Returning an empty JSON array from
  # Get-RequiredProperty would be unrolled by PowerShell into $null, while a
  # one-item array would be unrolled into its scalar value.
  $raw = $propertyValue.Value
  if ($raw -isnot [Array]) {
    throw "Company $Code contains an invalid $Property list."
  }
  $values = @($raw)
  $unique = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
  foreach ($value in $values) {
    if ($value -isnot [string] -or $value -cnotmatch '^type[1-7]$' -or -not $unique.Add($value)) {
      throw "Company $Code contains an invalid $Property list."
    }
  }
  return $values
}

function Assert-MobilePayloadContract(
  [object]$Manifest,
  [object]$Catalogue,
  [object]$Signals,
  [DateTimeOffset]$ShanghaiNow
) {
  if ((ConvertTo-RequiredInteger (Get-RequiredProperty $Manifest 'schema_version' 'Published manifest') 'manifest schema_version') -ne 1) {
    throw 'Published manifest schema version is unsupported by the Android client.'
  }
  if ([string](Get-RequiredProperty $Manifest 'product' 'Published manifest') -cne 'DS_DCF') {
    throw 'Published manifest product identity is invalid.'
  }
  if ($ExpectedSourceCommit -cnotmatch '^[0-9a-f]{40}$') {
    throw 'The running main source commit is unavailable or invalid.'
  }
  $manifestProvenance = Get-RequiredProperty $Manifest 'provenance' 'Published manifest provenance'
  if ([string](Get-RequiredProperty $manifestProvenance 'source_commit' 'Published manifest provenance') -cne $ExpectedSourceCommit) {
    throw 'Published generation was produced by another main source commit.'
  }
  Assert-SharedSnapshotFields $Manifest $Catalogue 'Published catalogue'
  Assert-SharedSnapshotFields $Manifest $Signals 'Published signals'

  $dataTimestamp = [DateTimeOffset]::Parse(
    [string](Get-RequiredProperty $Manifest 'data_timestamp_utc' 'Published manifest'),
    [Globalization.CultureInfo]::InvariantCulture,
    [Globalization.DateTimeStyles]::RoundtripKind
  )
  if ($dataTimestamp -gt $ShanghaiNow.ToUniversalTime().Add($script:MaximumFutureClockSkew)) {
    throw 'Published generation timestamp is later than the Android future-clock allowance.'
  }

  $companies = @(Get-RequiredProperty $Catalogue 'companies' 'Published catalogue')
  $companyCount = ConvertTo-RequiredInteger (
    Get-RequiredProperty $Catalogue 'company_count' 'Published catalogue'
  ) 'catalogue company_count'
  if (
    $companyCount -ne $companies.Count -or
    $companyCount -lt $script:MinimumCompanyCount -or
    $companyCount -gt $script:MaximumCompanyCount
  ) {
    throw 'Published catalogue company count is outside the Android acceptance range.'
  }
  Assert-AnalysisQuality $Manifest $companyCount 'Published manifest'
  Assert-AnalysisQuality $Catalogue $companyCount 'Published catalogue'
  Assert-AnalysisQuality $Signals $companyCount 'Published signals'

  $typeNames = Get-RequiredProperty $Catalogue 'type_names' 'Published catalogue'
  $typeNameProperties = @($typeNames.PSObject.Properties)
  $typeLabels = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
  if ($typeNameProperties.Count -ne 7) {
    throw 'Published catalogue does not define exactly seven type names.'
  }
  for ($number = 1; $number -le 7; $number++) {
    $typeKey = "type$number"
    $label = Get-RequiredProperty $typeNames $typeKey 'Published catalogue type names'
    if ($label -isnot [string] -or [string]::IsNullOrWhiteSpace($label) -or $label.Trim().Length -gt 40 -or -not $typeLabels.Add($label.Trim())) {
      throw 'Published catalogue contains an invalid or duplicate type name.'
    }
  }

  $actualCoverage = @{}
  for ($number = 1; $number -le 7; $number++) {
    $typeKey = "type$number"
    $counts = @{}
    foreach ($status in $script:ValidTypeStatuses) {
      $counts[$status] = 0L
    }
    $actualCoverage[$typeKey] = $counts
  }
  $companyCodes = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
  $candidateCodes = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
  $triggeredCount = 0L
  $conditionalCount = 0L
  foreach ($company in $companies) {
    $code = [string](Get-RequiredProperty $company 'code' 'Published company')
    if ($code -cnotmatch '^[036][0-9]{5}$' -or -not $companyCodes.Add($code)) {
      throw 'Published catalogue contains a non-SH/SZ or duplicate company code.'
    }
    $priceProperty = $company.PSObject.Properties['price']
    if ($null -ne $priceProperty -and $null -ne $priceProperty.Value) {
      if ((ConvertTo-RequiredFiniteNumber $priceProperty.Value "Company $code price") -le 0.0) {
        throw "Company $code contains a non-positive price."
      }
    }
    $buyTypes = @(Assert-StringTypeList $company 'buy_types' $code)
    $conditionalTypes = @(Assert-StringTypeList $company 'conditional_types' $code)
    if ($buyTypes.Count -gt 0) {
      $triggeredCount++
      [void]$candidateCodes.Add($code)
    }
    if ($conditionalTypes.Count -gt 0) {
      $conditionalCount++
      [void]$candidateCodes.Add($code)
    }
    $types = Get-RequiredProperty $company 'types' "Company $code"
    if (@($types.PSObject.Properties).Count -ne 7) {
      throw "Company $code does not contain exactly seven type states."
    }
    for ($number = 1; $number -le 7; $number++) {
      $typeKey = "type$number"
      $type = Get-RequiredProperty $types $typeKey "Company $code type states"
      $status = [string](Get-RequiredProperty $type 'status' "Company $code $typeKey")
      if ($status -cnotin $script:ValidTypeStatuses) {
        throw "Company $code contains an unrecognized type status."
      }
      $scoreProperty = $type.PSObject.Properties['score']
      if ($null -ne $scoreProperty -and $null -ne $scoreProperty.Value) {
        $score = ConvertTo-RequiredFiniteNumber $scoreProperty.Value "Company $code $typeKey score"
        if ($score -lt 0.0 -or $score -gt 10.0) {
          throw "Company $code contains a score outside 0 to 10."
        }
      }
      $reasonProperty = $type.PSObject.Properties['reason']
      $reason = if ($null -eq $reasonProperty -or $null -eq $reasonProperty.Value) { '' } else { [string]$reasonProperty.Value }
      if ($reason.Trim().Length -gt 200) {
        throw "Company $code contains an overlong public reason."
      }
      if (($typeKey -cin $buyTypes) -ne ($status -ceq 'triggered') -or ($typeKey -cin $conditionalTypes) -ne ($status -ceq 'conditional')) {
        throw "Company $code buy markers disagree with its type states."
      }
      $actualCoverage[$typeKey][$status]++
    }
  }

  $signalRows = @(Get-RequiredProperty $Signals 'signals' 'Published signals')
  $signalCodes = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
  foreach ($signal in $signalRows) {
    $code = [string](Get-RequiredProperty $signal 'code' 'Published signal')
    $detail = [string](Get-RequiredProperty $signal 'detail_text' "Signal $code")
    if ($code -cnotmatch '^[0-9]{6}$' -or [string]::IsNullOrWhiteSpace($detail) -or -not $signalCodes.Add($code)) {
      throw 'Published signals contain an invalid or duplicate company detail.'
    }
  }
  if (-not $candidateCodes.SetEquals($signalCodes)) {
    throw 'Published signals do not match the triggered and conditional company set.'
  }
  foreach ($entry in @(
    @($Signals, 'candidate_detail_count', $signalRows.Count),
    @($Signals, 'triggered_company_count', $triggeredCount),
    @($Signals, 'conditional_company_count', $conditionalCount)
  )) {
    if ((ConvertTo-RequiredInteger (Get-RequiredProperty $entry[0] $entry[1] 'Published signals') "signals $($entry[1])") -ne $entry[2]) {
      throw "Published signals $($entry[1]) is inconsistent."
    }
  }
  $summary = Get-RequiredProperty $Manifest 'summary' 'Published manifest'
  foreach ($entry in @(
    @('company_count', $companyCount),
    @('triggered_company_count', $triggeredCount),
    @('conditional_company_count', $conditionalCount),
    @('candidate_detail_count', $signalRows.Count)
  )) {
    if ((ConvertTo-RequiredInteger (Get-RequiredProperty $summary $entry[0] 'Published manifest summary') "summary $($entry[0])") -ne $entry[1]) {
      throw "Published manifest summary $($entry[0]) is inconsistent."
    }
  }

  $manifestCoverage = Get-RequiredProperty $summary 'type_coverage' 'Published manifest summary'
  $catalogueCoverage = Get-RequiredProperty $Catalogue 'coverage' 'Published catalogue'
  foreach ($coverage in @($manifestCoverage, $catalogueCoverage)) {
    if (@($coverage.PSObject.Properties).Count -ne 7) {
      throw 'Published type coverage does not contain exactly seven frameworks.'
    }
    for ($number = 1; $number -le 7; $number++) {
      $typeKey = "type$number"
      $declared = Get-RequiredProperty $coverage $typeKey 'Published type coverage'
      if (@($declared.PSObject.Properties).Count -ne $script:ValidTypeStatuses.Count) {
        throw "Published $typeKey coverage has an invalid status shape."
      }
      foreach ($status in $script:ValidTypeStatuses) {
        $count = ConvertTo-RequiredInteger (
          Get-RequiredProperty $declared $status "Published $typeKey coverage"
        ) "Published $typeKey $status coverage"
        if ($count -ne $actualCoverage[$typeKey][$status]) {
          throw "Published $typeKey $status coverage differs from the catalogue."
        }
      }
    }
  }
}

function Assert-ArchivedGeneration([string]$Working, [string]$ManifestPath, [string]$SignatureName, [string]$SignaturePath) {
  $archivedManifest = Join-Path $Working 'archived-manifest.json'
  $archivedSignature = Join-Path $Working "archived-$SignatureName"
  $archivedChecksums = Join-Path $Working 'archived-SHA256SUMS.txt'
  Copy-ArchiveFile 'manifest.json' $archivedManifest $script:MaximumManifestBytes
  Copy-ArchiveFile $SignatureName $archivedSignature 1KB
  Copy-ArchiveFile 'SHA256SUMS.txt' $archivedChecksums 4KB
  $manifestHash = (Get-FileHash -LiteralPath $ManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
  $signatureHash = (Get-FileHash -LiteralPath $SignaturePath -Algorithm SHA256).Hash.ToLowerInvariant()
  if (
    (Get-FileHash -LiteralPath $archivedManifest -Algorithm SHA256).Hash.ToLowerInvariant() -cne $manifestHash -or
    (Get-FileHash -LiteralPath $archivedSignature -Algorithm SHA256).Hash.ToLowerInvariant() -cne $signatureHash
  ) {
    throw 'Archived completion marker does not match the live signed generation.'
  }
  $expectedChecksums = @(
    "$manifestHash  manifest.json",
    "$signatureHash  $SignatureName"
  )
  $actualChecksums = @(Get-Content -LiteralPath $archivedChecksums -Encoding ascii)
  if (Compare-Object $actualChecksums $expectedChecksums -SyncWindow 0) {
    throw 'Archived completion checksums do not match the live signed generation.'
  }
}

function Test-PublishedGeneration([DateTimeOffset]$ShanghaiNow) {
  $working = Join-Path ([IO.Path]::GetTempPath()) ("ds-dcf-mobile-guard-" + [Guid]::NewGuid().ToString('N'))
  New-Item -ItemType Directory -Path $working | Out-Null
  try {
    $localManifest = Join-Path $working 'manifest.json'
    if (-not [string]::IsNullOrWhiteSpace($ManifestPath)) {
      if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
        throw 'Published manifest is unavailable.'
      }
      Copy-Item -LiteralPath $ManifestPath -Destination $localManifest
    } else {
      Copy-RemoteFile "${ManifestUrl}?workflow_guard=$([Uri]::EscapeDataString([string]$ShanghaiNow.ToUnixTimeSeconds()))" (
        $localManifest
      ) $script:MaximumManifestBytes
    }
    $manifest = Read-StrictJsonFile $localManifest $script:MaximumManifestBytes 'Published manifest'
    $today = $ShanghaiNow.ToString('yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture)
    if ([string]$manifest.market_as_of -cne $today -or $manifest.analysis_quality.ok -ne $true) {
      throw 'Published manifest is not a passing generation for today.'
    }
    $dataTimestamp = [DateTimeOffset]::Parse(
      [string]$manifest.data_timestamp_utc,
      [Globalization.CultureInfo]::InvariantCulture,
      [Globalization.DateTimeStyles]::RoundtripKind
    )
    $dataShanghai = [TimeZoneInfo]::ConvertTime($dataTimestamp, $script:ShanghaiZone)
    if ($dataShanghai.Date -ne $ShanghaiNow.Date -or $dataShanghai.TimeOfDay -lt $script:PostCloseReadyTime) {
      throw 'Published manifest is not a same-day post-close generation.'
    }

    $catalogueName = [string]$manifest.catalogue.filename
    if ($catalogueName -cnotmatch '^catalog-(?<generation>[0-9a-f]{16})\.json\.gz$') {
      throw 'Published catalogue filename is invalid.'
    }
    $generation = $Matches.generation
    $signalsName = "signals-$generation.json.gz"
    $signatureName = "manifest-$generation.sig"
    if (
      [string]$manifest.signals.filename -cne $signalsName -or
      [string]$manifest.signature.filename -cne $signatureName -or
      [string]$manifest.signature.algorithm -cne 'ECDSA_P256_SHA256'
    ) {
      throw 'Published manifest does not bind one signed generation.'
    }
    $catalogueMetadata = Get-RequiredAssetMetadata $manifest 'catalogue' $catalogueName
    $signalsMetadata = Get-RequiredAssetMetadata $manifest 'signals' $signalsName

    $cataloguePath = Join-Path $working $catalogueName
    $signalsPath = Join-Path $working $signalsName
    $signaturePath = Join-Path $working $signatureName
    Copy-GenerationFile $catalogueName $cataloguePath $script:MaximumPayloadBytes
    Copy-GenerationFile $signalsName $signalsPath $script:MaximumPayloadBytes
    Copy-GenerationFile $signatureName $signaturePath 1KB
    foreach ($entry in @(
      @($cataloguePath, $catalogueMetadata, 'catalogue'),
      @($signalsPath, $signalsMetadata, 'signals')
    )) {
      $item = Get-Item -LiteralPath $entry[0]
      $hash = (Get-FileHash -LiteralPath $entry[0] -Algorithm SHA256).Hash.ToLowerInvariant()
      if ([long]$entry[1].size -ne $item.Length -or [string]$entry[1].sha256 -cne $hash) {
        throw "Published $($entry[2]) bytes do not match the manifest."
      }
    }
    if ((Get-FileHash -LiteralPath $cataloguePath -Algorithm SHA256).Hash -ceq (
      Get-FileHash -LiteralPath $signalsPath -Algorithm SHA256
    ).Hash) {
      throw 'Published catalogue and signals unexpectedly have identical bytes.'
    }

    $androidSource = Get-Content -LiteralPath $AndroidSourcePath -Raw -Encoding utf8
    $keyMatch = [regex]::Match(
      $androidSource,
      'MOBILE_SIGNING_PUBLIC_KEY_BASE64\s*=\s*"(?<key>[A-Za-z0-9+/=]+)"\s*;',
      [Text.RegularExpressions.RegexOptions]::Singleline
    )
    if (-not $keyMatch.Success) {
      throw 'Android client signing key is unavailable.'
    }
    $publicKey = [Convert]::FromBase64String($keyMatch.Groups['key'].Value)
    $verifier = [Security.Cryptography.ECDsa]::Create()
    try {
      $bytesRead = 0
      $verifier.ImportSubjectPublicKeyInfo($publicKey, [ref]$bytesRead)
      $valid = $bytesRead -eq $publicKey.Length -and $verifier.VerifyData(
        [IO.File]::ReadAllBytes($localManifest),
        [IO.File]::ReadAllBytes($signaturePath),
        [Security.Cryptography.HashAlgorithmName]::SHA256,
        [Security.Cryptography.DSASignatureFormat]::Rfc3279DerSequence
      )
      if (-not $valid) {
        throw 'Published manifest signature does not match the Android client key.'
      }
    } finally {
      $verifier.Dispose()
    }
    $catalogue = Expand-StrictGzipJson $cataloguePath 'Published catalogue'
    $signals = Expand-StrictGzipJson $signalsPath 'Published signals'
    Assert-MobilePayloadContract $manifest $catalogue $signals $ShanghaiNow
    Assert-ArchivedGeneration $working $localManifest $signatureName $signaturePath
    return $true
  } catch {
    Write-Warning "The existing published generation cannot suppress a refresh: $($_.Exception.Message)"
    return $false
  } finally {
    if (Test-Path -LiteralPath $working) {
      Get-ChildItem -LiteralPath $working -File | Remove-Item -Force
      Remove-Item -LiteralPath $working -Force
    }
  }
}

$shanghaiNow = Get-ShanghaiNow
$calendarDecision = Get-CalendarDecision $shanghaiNow
if ($calendarDecision.closed) {
  Write-WorkflowDecision $false $calendarDecision.reason
  return
}
if ($EventName -ceq 'workflow_dispatch') {
  if ($shanghaiNow.TimeOfDay -lt $script:PostCloseReadyTime) {
    Write-WorkflowDecision $false 'manual_dispatch_before_post_close_window'
    return
  }
  Write-WorkflowDecision $true 'manual_dispatch_forced'
  return
}
if (Test-PublishedGeneration $shanghaiNow) {
  Write-WorkflowDecision $false 'current_signed_generation_already_published'
  return
}
Write-WorkflowDecision $true $calendarDecision.reason
