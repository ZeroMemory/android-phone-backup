param(
    [Parameter(Mandatory = $true)][string] $SdkRoot,
    [string] $Platform = 'android-34',
    [string] $BuildTools = '34.0.0',
    [string] $OutputDirectory = (Join-Path $PSScriptRoot 'build-vcard-reader')
)
$ErrorActionPreference = 'Stop'
$androidJar = Join-Path $SdkRoot "platforms\$Platform\android.jar"
$d8 = Join-Path $SdkRoot "build-tools\$BuildTools\d8.bat"
foreach ($dependency in @($androidJar, $d8)) {
    if (-not (Test-Path -LiteralPath $dependency -PathType Leaf)) { throw "Missing SDK component: $dependency" }
}
$javac = (Get-Command javac.exe -ErrorAction Stop).Source
if (Test-Path -LiteralPath $OutputDirectory) { throw 'Choose a new output directory; existing output is not overwritten.' }
New-Item -ItemType Directory -Path $OutputDirectory | Out-Null
$classes = Join-Path $OutputDirectory 'classes'
New-Item -ItemType Directory -Path $classes | Out-Null
& $javac --release 8 -classpath $androidJar -d $classes (Join-Path $PSScriptRoot 'vcard_reader\src\ReadContactVCard.java')
if ($LASTEXITCODE -ne 0) { throw 'Java compilation failed.' }
& $d8 --min-api 26 --lib $androidJar --output $OutputDirectory (Join-Path $classes 'ReadContactVCard.class')
if ($LASTEXITCODE -ne 0) { throw 'DEX compilation failed.' }
Write-Host "Built: $(Join-Path $OutputDirectory 'classes.dex')"
