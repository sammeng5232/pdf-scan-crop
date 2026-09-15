# Build PDF裁边.exe from PDF裁边.spec and deploy it to the folder the desktop shortcut runs.
#   .\build_pdf_crop_exe.ps1              build, then mirror into %USERPROFILE%\PDF裁边
#   .\build_pdf_crop_exe.ps1 -NoDeploy    build only (output stays in %TEMP%\pdf_crop_dist)
param(
    [string]$Deploy = (Join-Path $env:USERPROFILE "PDF裁边"),
    [switch]$NoDeploy
)
$ErrorActionPreference = "Stop"
$Project = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Project

function Invoke-Native([scriptblock]$Command, [string]$What) {
    # pip and PyInstaller log to stderr; Windows PowerShell 5.1 would turn each
    # such line into a terminating error under "Stop", so judge by exit code.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $Command } finally { $ErrorActionPreference = $previous }
    if ($LASTEXITCODE) { throw "$What failed with exit code $LASTEXITCODE" }
}

$Py314 = Join-Path $env:USERPROFILE "AppData\Local\Programs\Python\Python314\python.exe"
$Py = if (Test-Path $Py314) { $Py314 } else { (Get-Command python).Source }
Write-Host "Using $Py"
Invoke-Native { & $Py -m pip install -q -r "$Project\pdf_crop_requirements.txt" pyinstaller } "pip install"

$Dist = Join-Path $env:TEMP "pdf_crop_dist"
$Work = Join-Path $env:TEMP "pdf_crop_build"
Invoke-Native { & $Py -m PyInstaller --noconfirm --clean --distpath $Dist --workpath $Work "$Project\PDF裁边.spec" } "PyInstaller"
$App = Join-Path $Dist "PDF裁边"

# OCR language data and the user guide ship beside the exe, plus the source it was built from.
Copy-Item "$Project\PDF裁边使用说明.txt" "$App\使用说明.txt" -Force
New-Item -ItemType Directory -Force "$App\tessdata\configs" | Out-Null
# The repo keeps tessdata at its root, one level above this source folder.
$Tessdata = if (Test-Path "$Project\tessdata") { "$Project\tessdata" } else { Join-Path (Split-Path -Parent $Project) "tessdata" }
Copy-Item "$Tessdata\*.traineddata" "$App\tessdata\" -Force
if (Test-Path "$Tessdata\configs") {
    Copy-Item "$Tessdata\configs\*" "$App\tessdata\configs\" -Force
}
New-Item -ItemType Directory -Force "$App\source" | Out-Null
$Sources = "pdf_crop_app.py", "fix_pdf_edges.py", "PDF裁边.spec", "build_pdf_crop_exe.ps1",
    "pdf_crop_requirements.txt", "pdf_crop_version.txt", "pdf_crop.ico", "PDF裁边使用说明.txt"
foreach ($name in $Sources) {
    Copy-Item "$Project\$name" "$App\source\$name" -Force
}
Write-Host "Built: $App\PDF裁边.exe"
if ($NoDeploy) { return }

# Mirror into the deployed folder, leaving its git repo, README and .gitignore alone.
robocopy $App $Deploy /MIR /XD .git /XF README.md .gitignore /NFL /NDL /NJH /NP | Out-Host
if ($LASTEXITCODE -ge 8) { throw "robocopy failed with exit code $LASTEXITCODE" }
$global:LASTEXITCODE = 0
Write-Host "Deployed: $Deploy\PDF裁边.exe"
