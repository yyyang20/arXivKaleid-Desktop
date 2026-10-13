param([Parameter(Mandatory=$true)][string]$PythonExe, [string]$LicenseInputManifest)
$ErrorActionPreference = 'Stop'
# 从干净冻结 C 构建最终待发布包；有效成品重复执行时原样复用。
# 不激活 Conda、不改永久 PATH；构建器会核验解释器身份和固定版本。
$buildScript = Join-Path $PSScriptRoot 'build_windows_portable.py'
$buildArguments = @('-X', 'utf8', '-B', $buildScript)
if ($LicenseInputManifest) { $buildArguments += @('--license-input-manifest', $LicenseInputManifest) }
& $PythonExe @buildArguments
if ($LASTEXITCODE -ne 0) { throw 'Windows portable build failed.' }
