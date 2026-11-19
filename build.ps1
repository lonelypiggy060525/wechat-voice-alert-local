$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# 依赖（已有 .venv 时跳过安装）
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    uv venv .venv --python 3.11
}
uv pip install --python .venv\Scripts\python.exe -r requirements.txt pyinstaller

.venv\Scripts\pyinstaller.exe --noconfirm --clean --windowed --onefile `
    --name WeChatDBAlert `
    --icon assets\icon.ico `
    --add-data "assets;assets" `
    --hidden-import win32com.client `
    --hidden-import pythoncom `
    --hidden-import PIL._tkinter_finder `
    --hidden-import uiautomation `
    --hidden-import comtypes `
    --hidden-import zstandard `
    --hidden-import wechatauto `
    --hidden-import wechatauto.db `
    --collect-submodules wechatauto `
    app.py

$dst = Join-Path $env:LOCALAPPDATA "WeChatDBAlert"
New-Item -ItemType Directory -Force -Path $dst | Out-Null
Copy-Item "dist\WeChatDBAlert.exe" $dst -Force

Write-Host ""
Write-Host "构建完成: dist\WeChatDBAlert.exe"
Write-Host "已复制到: $dst\WeChatDBAlert.exe"
