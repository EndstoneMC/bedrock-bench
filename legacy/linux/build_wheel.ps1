# Build the cp314 manylinux wheel of one commit, following reference_local_manylinux_wheel_build.
param([Parameter(Mandatory)] [string]$Commit, [Parameter(Mandatory)] [string]$Version, [Parameter(Mandatory)] [string]$Name,
      [string[]]$CopyFile = @(), [string]$ExpectInDiff = '')
$ErrorActionPreference = 'Continue'
$S = 'C:\Users\Vincent\AppData\Local\Temp\claude\d--Projects-Endstone-endstone\7967334b-4dde-4367-ae32-73653b87ea94\scratchpad'
$repo = 'D:\Projects\Endstone\endstone'
$wt = Join-Path $S "wt-linux-$Name"
$out = Join-Path $S "dist-linux-$Name"

Get-ChildItem -Path $out -Filter *.whl -ErrorAction SilentlyContinue | Remove-Item -Force
git -C $repo worktree add --detach $wt $Commit
if ($LASTEXITCODE) { throw "worktree add failed" }
$rc = 1
try {
    Write-Output ("worktree HEAD " + (git -C $wt rev-parse HEAD))
    foreach ($f in $CopyFile) {
        Copy-Item -LiteralPath (Join-Path $repo $f) -Destination (Join-Path $wt $f) -Force
    }
    if ($CopyFile.Count) {
        $numstat = @(git -C $wt diff --numstat)
        Write-Output "worktree diff --numstat:"
        $numstat | ForEach-Object { Write-Output "  $_" }
        git -C $wt diff | ForEach-Object { Write-Output "  | $_" }
        $files = @($numstat | ForEach-Object { ($_ -split "`t")[2] })
        $diff = (git -C $wt diff) -join "`n"
        if (Compare-Object ($files | Sort-Object) ($CopyFile | ForEach-Object { $_.Replace([string][char]92, '/') } | Sort-Object)) {
            Write-Output "DIFF CHECK FAILED: changed files differ from the copied files"
            exit 3
        }
        if ($ExpectInDiff -and -not $diff.Contains($ExpectInDiff)) {
            Write-Output "DIFF CHECK FAILED: '$ExpectInDiff' not in the diff"
            exit 3
        }
        Write-Output "diff check ok"
    }
    $env:CIBW_BUILD = 'cp314-manylinux_x86_64'
    $env:CIBW_CONTAINER_ENGINE = 'docker; create_args: --volume D:/Projects/Endstone/endstone/.conan2:/project/.conan2'
    $env:SETUPTOOLS_SCM_PRETEND_VERSION = $Version
    $env:CIBW_ENVIRONMENT_PASS_LINUX = 'SETUPTOOLS_SCM_PRETEND_VERSION'
    $env:CIBW_TEST_COMMAND = 'true'
    Push-Location $wt
    try {
        uv run --no-project --with cibuildwheel cibuildwheel --platform linux --output-dir $out
        $rc = $LASTEXITCODE
        Write-Output "cibuildwheel exit=$rc"
    }
    finally {
        Pop-Location
    }
}
finally {
    git -C $repo worktree remove --force $wt
    Write-Output "worktree removed exit=$LASTEXITCODE"
}
exit $rc
