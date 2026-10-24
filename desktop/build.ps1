param([switch]$Install, [string]$Workspace = (Join-Path ([Environment]::GetFolderPath('MyDocuments')) '剪辑工作台'))
$ErrorActionPreference = 'Stop'
$engineRoot = Split-Path $PSScriptRoot
$enginePython = Join-Path $env:LOCALAPPDATA 'JianyingEngine\venv\Scripts\python.exe'
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$output = Join-Path $engineRoot 'dist\desktop'
New-Item -ItemType Directory -Path $output -Force | Out-Null
$icon = Join-Path $engineRoot 'src\jianying_engine\web\app.ico'
$iconCode = @'
import sys
from PIL import Image, ImageDraw
image = Image.new('RGBA', (256, 256), (0, 0, 0, 0))
d = ImageDraw.Draw(image)
d.rounded_rectangle((8, 8, 248, 248), radius=52, fill='#29362f')
d.rounded_rectangle((49, 59, 207, 197), radius=12, outline='#e2e9d7', width=12)
d.line((81, 63, 81, 193), fill='#e2e9d7', width=9)
d.line((175, 63, 175, 193), fill='#e2e9d7', width=9)
for y in (88, 128, 168):
    d.line((53, y, 77, y), fill='#e2e9d7', width=8)
    d.line((179, y, 203, y), fill='#e2e9d7', width=8)
d.polygon([(106, 96), (106, 162), (155, 129)], fill='#afc193')
image.save(sys.argv[1], sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])
'@
$iconCode | & $enginePython - $icon
if ($LASTEXITCODE -ne 0) { throw 'Icon build failed' }
$exe = Join-Path $output '剪辑工作台.exe'
& $compiler /nologo /target:winexe /platform:x64 /optimize+ /reference:System.Windows.Forms.dll "/win32icon:$icon" "/out:$exe" (Join-Path $PSScriptRoot 'Launcher.cs')
if ($LASTEXITCODE -ne 0) { throw 'Launcher compile failed' }
if ($Install) {
    $destination = Join-Path $env:LOCALAPPDATA 'Programs\JianyingWorkbench'
    New-Item -ItemType Directory -Path $destination -Force | Out-Null
    Copy-Item -LiteralPath $exe -Destination (Join-Path $destination '剪辑工作台.exe') -Force
    New-Item -ItemType Directory -Path $Workspace -Force | Out-Null
    $shell = New-Object -ComObject WScript.Shell
    $targets = @((Join-Path ([Environment]::GetFolderPath('Desktop')) '剪辑工作台.lnk'), (Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs\剪辑工作台.lnk'))
    foreach ($target in $targets) {
        $link = $shell.CreateShortcut($target)
        $link.TargetPath = Join-Path $destination '剪辑工作台.exe'
        $link.Arguments = '--workspace "' + $Workspace.TrimEnd('\') + '"'
        $link.WorkingDirectory = $Workspace
        $link.Description = '本机 Agent 驱动的剪辑工作台'
        $link.IconLocation = $link.TargetPath + ',0'
        $link.Save()
    }
    Write-Output "Installed: $destination"
}
Get-FileHash -LiteralPath $exe -Algorithm SHA256
