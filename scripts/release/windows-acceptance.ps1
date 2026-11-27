param(
    [Parameter(Mandatory)] [ValidateSet('U08', 'Scheduler')] [string] $Scenario,
    [Parameter(Mandatory)] [string] $EvidenceDirectory
)

$ErrorActionPreference = 'Stop'
if (!$IsWindows) { throw 'Native Windows is required' }
if ($Scenario -eq 'U08') {
    $arguments = @(
        'tests/v2/release/test_update.py', '-v',
        'UpdateAcceptance.test_u08_native_stdio_holds_executable_until_it_closes',
        'UpdateAcceptance.test_u08_managed_update_sharing_violation_and_interrupted_swap_recovery'
    )
    $testCount = 2
} else {
    $arguments = @('tests/v2/release/test_windows_launcher.py', '-v')
    $testCount = 1
}

$name = $Scenario.ToLowerInvariant()
New-Item -ItemType Directory -Force -Path $EvidenceDirectory | Out-Null
$log = Join-Path $EvidenceDirectory "$name.log"
$receipt = Join-Path $EvidenceDirectory "$name.json"
if (Test-Path -LiteralPath $receipt) { throw 'Use a fresh evidence directory for each run' }
$env:PYTHONPATH = 'tests/v2'
python -B @arguments 2>&1 | Tee-Object -FilePath $log
$testExitCode = $LASTEXITCODE
$output = Get-Content -LiteralPath $log -Raw
if ($testExitCode -ne 0 -or $output -notmatch '(?m)^OK\s*$' -or
    $output -notmatch "(?m)^Ran $testCount tests? in ") {
    throw "$Scenario did not pass all $testCount native tests without skips"
}

$architecture = [Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
@{
    scope = 'isolated-test-signing'; scenario = $Scenario; status = 'PASS'; tests = $testCount
    architecture = $architecture; target = $env:TARGET; legacy_commit = $env:LEGACY_COMMIT
    reviewed_legacy_commit = $env:REVIEWED_LEGACY_COMMIT
    legacy_rust_tree = $env:LEGACY_RUST_TREE; legacy_web_tree = $env:LEGACY_WEB_TREE
    commit = $env:BMEM_BUILD_COMMIT; windows_version = [Environment]::OSVersion.Version.ToString()
    legacy_sha256 = (Get-FileHash -LiteralPath $env:BMEM_UPDATE_LEGACY_BIN).Hash.ToLowerInvariant()
    initial_sha256 = (Get-FileHash -LiteralPath $env:BMEM_UPDATE_TEST_BIN).Hash.ToLowerInvariant()
    updated_sha256 = (Get-FileHash -LiteralPath $env:BMEM_UPDATE_NEXT_BIN).Hash.ToLowerInvariant()
    log_sha256 = (Get-FileHash -LiteralPath $log).Hash.ToLowerInvariant()
} | ConvertTo-Json | Set-Content -LiteralPath $receipt -Encoding utf8NoBOM
