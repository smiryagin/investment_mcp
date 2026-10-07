[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$SourceServiceName,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$TargetServiceName,

    [switch]$RestartTargetService
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$servicesRoot = 'HKLM:\SYSTEM\CurrentControlSet\Services'
$sqlSettingPattern = '^\s*SQLSERVER_[A-Za-z0-9_]*\s*='

function Get-ServiceParameters {
    param([Parameter(Mandatory = $true)][string]$ServiceName)

    $key = Join-Path $servicesRoot "$ServiceName\Parameters"
    if (-not (Test-Path -LiteralPath $key -PathType Container)) {
        throw "NSSM parameters were not found for service '$ServiceName'."
    }

    Get-ItemProperty -LiteralPath $key
}

function Get-OptionalPropertyValues {
    param(
        [Parameter(Mandatory = $true)]$InputObject,
        [Parameter(Mandatory = $true)][string]$PropertyName
    )

    $property = $InputObject.PSObject.Properties[$PropertyName]
    if ($null -eq $property) {
        return @()
    }

    return @($property.Value)
}

function Get-SqlSettings {
    param(
        [Parameter(Mandatory = $true)]$ServiceParameters,
        [Parameter(Mandatory = $true)][string]$ServiceName
    )

    $serviceDirectory = [string]$ServiceParameters.AppDirectory
    if (-not $serviceDirectory) {
        throw "Service '$ServiceName' does not define AppDirectory."
    }

    $envFile = Join-Path $serviceDirectory '.env'
    if (Test-Path -LiteralPath $envFile -PathType Leaf) {
        $fileSettings = @(
            Get-Content -LiteralPath $envFile |
                Where-Object { $_ -is [string] -and $_ -match $sqlSettingPattern }
        )
        if ($fileSettings.Count -gt 0) {
            return [pscustomobject]@{
                Entries = [string[]]$fileSettings
                Source = $envFile
            }
        }
    }

    $serviceEnvironment = @(
        Get-OptionalPropertyValues `
            -InputObject $ServiceParameters `
            -PropertyName 'AppEnvironment'
        Get-OptionalPropertyValues `
            -InputObject $ServiceParameters `
            -PropertyName 'AppEnvironmentExtra'
    )
    $serviceSettings = @(
        $serviceEnvironment |
            Where-Object { $_ -is [string] -and $_ -match $sqlSettingPattern }
    )
    if ($serviceSettings.Count -gt 0) {
        return [pscustomobject]@{
            Entries = [string[]]$serviceSettings
            Source = "$ServiceName NSSM environment"
        }
    }

    throw "No SQLSERVER_* configuration was found for service '$ServiceName'."
}

$sourceParameters = Get-ServiceParameters -ServiceName $SourceServiceName
$targetParameters = Get-ServiceParameters -ServiceName $TargetServiceName
$sourceSql = Get-SqlSettings `
    -ServiceParameters $sourceParameters `
    -ServiceName $SourceServiceName

$targetDirectory = [string]$targetParameters.AppDirectory
if (-not $targetDirectory -or -not (Test-Path -LiteralPath $targetDirectory -PathType Container)) {
    throw "Target service directory '$targetDirectory' does not exist."
}

$targetEnvFile = Join-Path $targetDirectory '.env'
$targetLines = @()
if (Test-Path -LiteralPath $targetEnvFile -PathType Leaf) {
    $targetLines = @(Get-Content -LiteralPath $targetEnvFile)
}

$preservedLines = @(
    $targetLines |
        Where-Object { $_ -notmatch $sqlSettingPattern }
)
$updatedLines = [string[]]@(
    $sourceSql.Entries
    ''
    $preservedLines
)

$backupPath = $null
if ($PSCmdlet.ShouldProcess($targetEnvFile, 'Synchronize SQL Server configuration')) {
    if (Test-Path -LiteralPath $targetEnvFile -PathType Leaf) {
        $backupPath = "$targetEnvFile.backup-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
        Copy-Item -LiteralPath $targetEnvFile -Destination $backupPath
    }

    $utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines($targetEnvFile, $updatedLines, $utf8WithoutBom)

    if ($RestartTargetService) {
        Restart-Service -Name $TargetServiceName
    }
}

$copiedNames = @(
    $sourceSql.Entries |
        ForEach-Object { (($_ -split '=', 2)[0]).Trim() } |
        Sort-Object -Unique
)
$backupDisplay = if ($backupPath) { $backupPath } else { '(none)' }
$serviceStatus = if ($RestartTargetService) {
    (Get-Service -Name $TargetServiceName).Status
}
else {
    'Not restarted'
}

[pscustomobject]@{
    Source = $sourceSql.Source
    Destination = $targetEnvFile
    Backup = $backupDisplay
    CopiedVariables = $copiedNames -join ', '
    ServiceStatus = $serviceStatus
}
