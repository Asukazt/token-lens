param([switch]$PrepareOnly)

$ErrorActionPreference = "Stop"
$plugin = Join-Path $PSScriptRoot "plugins\api-balance"
$python = @(
    (Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"),
    (Join-Path $env:USERPROFILE ".codex\dependencies\python\python.exe")
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $python) {
    $command = Get-Command python -ErrorAction SilentlyContinue
    if ($command) { $python = $command.Source }
}
if (-not $python) { throw "Python 3.10+ is required." }
& $python -c "import sys, tkinter, PIL; assert sys.version_info >= (3, 10), 'Python 3.10+ is required'"
if ($LASTEXITCODE -ne 0) { throw "Python 3.10+, tkinter and Pillow are required. Install Pillow with: python -m pip install Pillow" }

$mcp = @{
    mcpServers = @{
        api_balance = @{
            command = $python
            args = @((Join-Path $plugin "server.py"))
            cwd = $plugin
            startup_timeout_sec = 30
            tool_timeout_sec = 120
        }
    }
}
$json = $mcp | ConvertTo-Json -Depth 5
[IO.File]::WriteAllText((Join-Path $plugin ".mcp.json"), $json, (New-Object Text.UTF8Encoding($false)))
if ($PrepareOnly) { Write-Host "Prepared local launch paths. Codex configuration was not changed."; exit 0 }

$codex = Get-Command codex -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source
if (-not $codex) {
    $codex = Get-ChildItem -LiteralPath (Join-Path $env:LOCALAPPDATA "OpenAI\Codex\bin") -Filter codex.exe -Recurse -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
}
if (-not $codex) { throw "Codex CLI was not found. Add codex to PATH and run this installer again." }
& $codex plugin marketplace add $PSScriptRoot
if ($LASTEXITCODE -ne 0) { throw "Could not register the plugin marketplace." }
& $codex plugin add api-balance@local
if ($LASTEXITCODE -ne 0) { throw "Could not install api-balance@local." }
Write-Host "Installed Token Lens. Restart Codex and enable the plugin in Settings."
