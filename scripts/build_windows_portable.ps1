param([Parameter(Mandatory=$true)][string]$PythonExe)
$ErrorActionPreference = 'Stop'
# 不激活 Conda、不改永久 PATH；构建器会核验解释器身份和固定版本。
$buildScript = Join-Path $PSScriptRoot 'build_windows_portable.py'
& $PythonExe -X utf8 -B $buildScript
if ($LASTEXITCODE -ne 0) { throw 'Windows portable build failed.' }
