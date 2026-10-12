# Build the manylinux wheel from perf-verify with ENDSTONE_VERIFY_PERF=ON, following reference_local_manylinux_wheel_build.
$ErrorActionPreference = 'Continue'
$S = 'C:\Users\Vincent\AppData\Local\Temp\claude\d--Projects-Endstone-endstone\7967334b-4dde-4367-ae32-73653b87ea94\scratchpad'
$repo = 'D:\Projects\Endstone\endstone'
$wt = Join-Path $S 'wt-linux-verify'
$out = Join-Path $S 'dist-linux-verify'
$version = '0.11.14.dev6'

Get-ChildItem -Path $out -Filter *.whl -ErrorAction SilentlyContinue | Remove-Item -Force
git -C $repo worktree add --detach $wt perf-verify
if ($LASTEXITCODE) { throw "worktree add failed" }
try {
    Write-Output ("worktree HEAD " + (git -C $wt rev-parse HEAD))
    # conan-py-build overrides tools.cmake.cmaketoolchain:extra_variables on its command line, so pass the macro instead
    $prof = "include(/project/.conan2/profiles/default)`n`n[conf]`ntools.build:defines=['ENDSTONE_VERIFY_PERF']`n"
    [IO.File]::WriteAllText((Join-Path $wt 'verify.profile'), $prof)

    $env:CIBW_BUILD = 'cp314-manylinux_x86_64'
    $env:CIBW_CONTAINER_ENGINE = 'docker; create_args: --volume D:/Projects/Endstone/endstone/.conan2:/project/.conan2'
    $env:SETUPTOOLS_SCM_PRETEND_VERSION = $version
    $env:CIBW_ENVIRONMENT_PASS_LINUX = 'SETUPTOOLS_SCM_PRETEND_VERSION'
    $env:CIBW_TEST_COMMAND = 'true'
    $env:CIBW_CONFIG_SETTINGS = 'host-profile=/project/verify.profile'
    Push-Location $wt
    try {
        uv run --no-project --with cibuildwheel cibuildwheel --platform linux --output-dir $out
        Write-Output "cibuildwheel exit=$LASTEXITCODE"
    }
    finally {
        Pop-Location
    }
}
finally {
    git -C $repo worktree remove --force $wt
    Write-Output "worktree removed exit=$LASTEXITCODE"
}
