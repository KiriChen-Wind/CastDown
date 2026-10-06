# Macast 音乐缓存插件安装器（Windows，免 Python）
# Copyright (C) 2026 KiriChen-Wind
# SPDX-License-Identifier: GPL-3.0-or-later
#
# 【重要】本文件保存为「UTF-8 带 BOM」。
# Windows PowerShell 5.1 在没有 BOM 时按系统 ANSI 代码页（中文系统为 GBK）解析
# .ps1 脚本，文件里的中文会变成乱码并直接导致语法错误、整个脚本无法执行。
# 用任何编辑器改完这个文件后，请确认仍保留 BOM。
# 验证：文件前三个字节应为 EF BB BF。
#
# 用法：
#   .\install.ps1                          安装
#   .\install.ps1 -CacheDir "D:\Music"     安装并预设缓存目录
#   .\install.ps1 -Select                  安装并切换 Macast 渲染器
#   .\install.ps1 -Uninstall               卸载
#   .\install.ps1 -Where                   只打印配置目录

[CmdletBinding()]
param(
  [string]$CacheDir = '',
  [switch]$Select,
  [switch]$Uninstall,
  [switch]$Where
)

$ErrorActionPreference = 'Stop'

# Macast 的 SETTING_DIR 来自 appdirs.user_config_dir('Macast', 'xfangfang')。
# 该方法的 roaming 参数默认是 False，Windows 上对应 CSIDL_LOCAL_APPDATA，
# 也就是 %LOCALAPPDATA%，**不是** %APPDATA%（Roaming 子目录）。
# 装到 Roaming 不会有任何报错，但 Macast 永远扫不到插件。
$Cfg = if ($env:MACAST_CONFIG_DIR) { $env:MACAST_CONFIG_DIR } else { Join-Path $env:LOCALAPPDATA 'xfangfang\Macast' }
$RendererDir = Join-Path $Cfg 'renderer'
$PluginName = 'music_cache.py'
$PluginTitle = '音乐缓存'
$Src = Join-Path $PSScriptRoot "renderer\$PluginName"
$SettingFile = Join-Path $Cfg 'macast_setting.json'

Write-Host "Macast 配置目录：$Cfg"
if ($Where) { exit 0 }

if ($Uninstall) {
  $dst = Join-Path $RendererDir $PluginName
  if (Test-Path $dst) { Remove-Item $dst -Force; Write-Host "移除：$dst" }
  else { Write-Host "提示：未安装：$dst" }
  exit 0
}

if (-not (Test-Path $Src)) { Write-Host "错误：找不到插件源文件：$Src"; exit 1 }

New-Item -ItemType Directory -Force -Path $RendererDir | Out-Null
$init = Join-Path $RendererDir '__init__.py'
if (-not (Test-Path $init)) { New-Item -ItemType File -Path $init | Out-Null }

Copy-Item $Src (Join-Path $RendererDir $PluginName) -Force
Write-Host "成功：插件已安装：$(Join-Path $RendererDir $PluginName)"

# 提示以前误装到 Roaming 的残留（Macast 不会读取该路径）
$Roaming = Join-Path $env:APPDATA 'xfangfang\Macast'
$Stale = Join-Path $Roaming "renderer\$PluginName"
if (Test-Path $Stale) {
  Write-Host "警告：发现旧的安装残留（Macast 不会读取此路径）：$Stale"
  Write-Host "    可安全删除：Remove-Item -Recurse -Force `"$Roaming`""
}

if ($Select -or $CacheDir) {
  # 读写一律走 .NET 并显式指定 UTF-8（无 BOM 写出）。
  # 用 Get-Content / Set-Content 会跟随宿主 ANSI 默认编码，
  # 在中文 Windows 上会静默把非 ASCII 值（例如中文 DLNA_FriendlyName）写成乱码。
  $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
  $json = $null
  if (Test-Path $SettingFile) {
    Copy-Item $SettingFile "$SettingFile.bak" -Force
    try {
      $text = [System.IO.File]::ReadAllText($SettingFile, $Utf8NoBom)
      $json = $text | ConvertFrom-Json
    } catch { Write-Host "警告：无法解析 macast_setting.json，跳过写入"; $json = $null }
  }
  if ($null -eq $json) { $json = New-Object psobject }

  function Set-Field($obj, $name, $value) {
    if (@($obj.PSObject.Properties.Name) -contains $name) { $obj.$name = $value }
    else { $obj | Add-Member -NotePropertyName $name -NotePropertyValue $value }
  }

  if ($Select) { Set-Field $json 'Macast_Renderer' $PluginTitle }
  if ($CacheDir) {
    New-Item -ItemType Directory -Force -Path $CacheDir | Out-Null
    Set-Field $json 'Cache_Dir' (Resolve-Path -LiteralPath $CacheDir).Path
  }
  New-Item -ItemType Directory -Force -Path $Cfg | Out-Null
  $out = $json | ConvertTo-Json -Depth 8

  # Macast 的 Setting.load() 用 open(path) 且不带 encoding，
  # 在中文 Windows 上按 cp936 解码。所以这个文件必须保持纯 ASCII：
  # 把所有非 ASCII 字符转义成 \uXXXX（Macast 自己也是 json.dump(ensure_ascii=True)）。
  $sb = New-Object System.Text.StringBuilder
  foreach ($ch in $out.ToCharArray()) {
    if ([int]$ch -lt 128) { [void]$sb.Append($ch) }
    else { [void]$sb.AppendFormat('\u{0:x4}', [int]$ch) }
  }
  [System.IO.File]::WriteAllText($SettingFile, $sb.ToString(), $Utf8NoBom)
  Write-Host "成功：已更新设置：$SettingFile"
}

Write-Host ""
Write-Host "下一步：重启 Macast，在托盘菜单「设置 → 选择播放器」里选「$PluginTitle」。"
Write-Host "缓存开关和目录在托盘菜单的「音乐缓存」分组里。"
