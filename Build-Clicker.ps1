param(
    [Parameter(Mandatory = $true)][string] $SdkRoot,
    [string] $Platform = 'android-34',
    [string] $BuildTools = '34.0.0',
    [string] $OutputDirectory = (Join-Path $PSScriptRoot 'build-clicker')
)
$ErrorActionPreference = 'Stop'
$androidJar = Join-Path $SdkRoot "platforms\$Platform\android.jar"
$automatorJar = Join-Path $SdkRoot "platforms\$Platform\uiautomator.jar"
$testBaseJar = Join-Path $SdkRoot "platforms\$Platform\optional\android.test.base.jar"
$d8 = Join-Path $SdkRoot "build-tools\$BuildTools\d8.bat"
foreach ($dependency in @($androidJar, $automatorJar, $testBaseJar, $d8)) {
    if (-not (Test-Path -LiteralPath $dependency -PathType Leaf)) { throw "Missing SDK component: $dependency" }
}
$javac = (Get-Command javac.exe -ErrorAction Stop).Source
$jar = (Get-Command jar.exe -ErrorAction Stop).Source
if (Test-Path -LiteralPath $OutputDirectory) { throw 'Choose a new output directory; existing output is not overwritten.' }
$classes = Join-Path $OutputDirectory 'classes'
$dex = Join-Path $OutputDirectory 'dex'
New-Item -ItemType Directory -Path $classes, $dex -Force | Out-Null
& $javac --release 8 -classpath "$androidJar;$automatorJar;$testBaseJar" -d $classes (Join-Path $PSScriptRoot 'uiauto_clicker\src\Clicker.java')
if ($LASTEXITCODE -ne 0) { throw 'Java compilation failed.' }
& $d8 --min-api 18 --lib $androidJar --classpath $automatorJar --classpath $testBaseJar --output $dex (Join-Path $classes 'Clicker.class')
if ($LASTEXITCODE -ne 0) { throw 'DEX compilation failed.' }
$output = Join-Path $OutputDirectory 'clicker.jar'
& $jar --create --file $output -C $dex classes.dex
if ($LASTEXITCODE -ne 0) { throw 'JAR creation failed.' }
Write-Host "Built: $output"
