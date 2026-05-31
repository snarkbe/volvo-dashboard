#Requires -Version 5.1
param(
    [Parameter(Mandatory=$true)]
    [ValidateSet('patch','minor','major')]
    [string]$BumpType
)

$ErrorActionPreference = 'Stop'

$versionFile = Join-Path $PSScriptRoot 'VERSION'
$current = (Get-Content $versionFile -Raw).Trim()

if ($current -notmatch '^(\d+)\.(\d+)\.(\d+)$') {
    Write-Error "Invalid version in VERSION file: $current"
    exit 1
}

[int]$major = $Matches[1]
[int]$minor = $Matches[2]
[int]$patch = $Matches[3]

switch ($BumpType) {
    'major' { $major++; $minor = 0; $patch = 0 }
    'minor' { $minor++; $patch = 0 }
    'patch' { $patch++ }
}

$next = "$major.$minor.$patch"
$tag  = "v$next"

Write-Host "Bumping $current -> $next"

Set-Content -Path $versionFile -Value "$next`n" -NoNewline:$false

git add VERSION
git commit -m "chore: release $tag"
git tag $tag
git push -u origin (git rev-parse --abbrev-ref HEAD)
git push origin $tag

Write-Host "Released $tag — GitHub Actions will build and push the Docker image."
