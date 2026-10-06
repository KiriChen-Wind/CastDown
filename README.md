<div align="center">

# Macast 音乐自动抓取插件

手机把音乐 DLNA 投送到电脑时，**在 mpv 照常播放的同时**把媒体流缓存到指定目录，
读取音频内嵌标签，按 `艺术家-歌名` 重命名，完成后弹出「缓存完毕」提示。

[![License](https://img.shields.io/badge/license-GPL--3.0--or--later-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.7%2B-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg)](#)
[![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen.svg)](#特性)

基于 [xfangfang/Macast](https://github.com/xfangfang/Macast) 的渲染器插件。
官方绿色版即插即用，不需要重新打包 Macast。

</div>

---

## 特性

- **不阻塞播放** —— 先交给 mpv 播放，缓存在后台线程并行下载，投送零等待。
- **零依赖** —— 只用 Python 标准库。官方 Macast 是 PyInstaller 冻结包，
  宿主里没有 mutagen 之类第三方库，因此标签解析为自实现。
- **元数据三级兜底** —— 内嵌标签 → DLNA `DIDL-Lite` → URL 文件名。
- **按文件头判定容器** —— 不信任 URL 后缀，下载完嗅探真实格式决定扩展名。
- **只缓存音频**（可关）—— 跳过 `video/*` 与视频后缀，不把电影写进音乐库。
- **原子落盘** —— 先写 `.macast_cache_tmp/*.part`，再 `os.replace` 到最终名；
  重名追加 ` (1)`，绝不覆盖已有文件。
- **关键帧音质无损** —— 只做下载与改名，不转码、不重封装。
- **Windows 安全文件名** —— 清洗非法字符、尾部点/空格、`CON`/`LPT1` 等保留名、长度上限。

## 环境要求

| 项目 | 要求 |
| --- | --- |
| Macast | [官方发行版](https://github.com/xfangfang/Macast/releases)（绿色版即可，免安装） |
| 系统 | Windows / macOS / Linux |
| Python | 仅安装时需要；用 `install.ps1` 则完全不需要 |

## 安装

```powershell
git clone https://github.com/<你的用户名>/<仓库名>.git
cd <仓库名>
```

**Windows**（任选其一）：

```powershell
.\install.ps1 -CacheDir "D:\Music" -Select     # 免 Python
python install.py --dir "D:\Music" --select    # 需要 Python 3
```

**macOS / Linux**：

```bash
python3 install.py --dir ~/Music --select
```

| 参数 | 作用 |
| --- | --- |
| `-CacheDir` / `--dir` | 预设缓存目录 |
| `-Select` / `--select` | 直接把 `Macast_Renderer` 写进配置，免去手动切换 |
| `-Uninstall` / `--uninstall` | 卸载插件（保留已缓存的音乐） |
| `-Where` / `--where` | 只打印 Macast 配置目录 |

安装目标就是 Macast 的配置目录：

| 平台 | 路径 |
| --- | --- |
| Windows | `%LOCALAPPDATA%\xfangfang\Macast` |
| macOS | `~/Library/Application Support/Macast` |
| Linux | `$XDG_CONFIG_HOME/Macast`（默认 `~/.config/Macast`） |

> **Windows 上是 `%LOCALAPPDATA%`（`AppData\Local`），不是 `%APPDATA%`（`AppData\Roaming`）。**
> `appdirs.user_config_dir()` 的 `roaming` 参数默认 `False`，对应 `CSIDL_LOCAL_APPDATA`。
> 装到 Roaming 不会有任何报错，但 Macast 永远扫不到。安装器会自动检测并提示 Roaming 下的旧残留。

**重启 Macast**，托盘菜单「设置 → 选择播放器」选 **音乐缓存**（默认是「MPV Renderer」）。

## 使用

托盘菜单里会出现「音乐缓存」分组：

| 菜单项 | 作用 |
| --- | --- |
| 启用音乐缓存 | 缓存总开关 |
| 仅缓存音频 | 只缓存音频，跳过视频（默认开） |
| 播放器内提示 | 缓存完成时同时在 mpv 画面里显示提示 |
| 文件名格式 | 艺术家-歌名 / 艺术家 - 歌名 / 歌名-艺术家 |
| 缓存目录 | 选择目录… / 打开缓存目录 / 恢复默认目录 / 当前路径 |

投送音乐后，缓存完成会弹系统通知，标题「缓存完毕」，正文是最终文件名。

## 设置项

写入 Macast 的 `macast_setting.json`，也可手工改（改前先退出 Macast）：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `Cache_Enable` | `1` | 总开关 |
| `Cache_Dir` | `%USERPROFILE%\Music\MacastCache`（macOS/Linux 为 `~/Music/MacastCache`） | 缓存目录 |
| `Cache_AudioOnly` | `1` | 仅缓存音频 |
| `Cache_Toast` | `1` | mpv 画面内提示 |
| `Cache_NameFormat` | `{artist}-{title}` | 支持 `{artist}` `{title}` `{album}` |
| `Cache_UserAgent` | 浏览器 UA | 部分 CDN 校验 UA，可改成投送 App 的 UA |
| `Cache_MaxSizeMB` | `512` | 单文件上限，防爆量 |

> 这个文件必须保持**纯 ASCII**（非 ASCII 字符转成 `\uXXXX`）。原因见下方「宿主约束」第 3 条。

## 工作原理

```
DLNA 客户端 ──SOAP SetAVTransportURI──▶ DLNAProtocol
                                            │
                                            ├─ set_state_url(uri)
                                            └─ renderer.set_media_url(uri)
                                                   │
                       ┌───────────────────────────┴───────────────────────────┐
                       ▼ 同步                                                  ▼ 后台线程
              mpv loadfile（立即播放）                          下载 → 嗅探容器 → 读标签
                                                               → 改名 → os.replace → 通知
```

插件继承 `MPVRenderer`，只重写 `set_media_url`：

```python
def set_media_url(self, url, start="0"):
    super().set_media_url(url, start)   # 先播放，绝不阻塞投送
    self._schedule(url)                 # 再排入后台缓存队列
```

元数据读取放在**下载完成之后**，天然错开了与 `CurrentTrackMetaData` 写入的竞态。

## 宿主约束

官方 Macast.exe 实测是 **PyInstaller 4.8 + Python 3.7.9 冻结包**。
下面这些约束违反了都**不会报错**——插件只是静默消失，或者悄悄改坏你的设置。
全部来自实测，不是推测。

### 1. 插件只能 import 宿主已打包的模块

用探针插件在真实宿主里实测的可用模块：

| 可用 | 不可用 |
| --- | --- |
| `os` `re` `sys` `uuid` `time` `gettext` `logging` `threading` `subprocess` `enum` | `xml.etree` |
| `urllib.request` `urllib.parse` `urllib.error` `http.client` `socket` | |
| `requests` `cherrypy` `lxml.etree` | |

`xml.etree.ElementTree` **不存在**（Macast 自己用的是 `lxml`），所以 DIDL 解析只能用正则。
`tests/test_music_cache.py` 的 4b 节用 AST 静态扫描导入表并比对这份白名单，防止回归。

### 2. 配置目录是 `%LOCALAPPDATA%` 而非 `%APPDATA%`

见上方安装说明。

### 3. `macast_setting.json` 必须保持纯 ASCII

Macast 的 `Setting.load()` 是 `open(path)` 且不带 `encoding=`，在中文 Windows 上按
系统 ANSI 代码页（cp936）解码。Macast 自己用 `json.dump(..., ensure_ascii=True)` 写，
所以一直没暴露问题；如果往里写裸 UTF-8，中文会被读成乱码再存回去
（例如设备名 `Macast(Kiri的PC)` 会变成 `Macast(Kiri鐨凴PC)`）。

两个安装器都按 `ensure_ascii=True` 语义输出，`tests/test_music_cache.py` 第 11 节锁住了这一点。

## 开发与测试

```bash
python tests/test_music_cache.py      # 离线验证：72 项
python tests/e2e_real_macast.py       # 真机验证：需要 Macast.exe 正在运行
```

**离线夹具**用桩模块替换 `cherrypy` / `macast.*` / `macast_renderer`，覆盖：
6 种容器的标签解析、容器嗅探、文件名清洗与去重、DIDL 正则解析、
起本地 HTTP 服务走完 `set_media_url → 落盘 → 重命名 → 通知` 全链路、
重复投送去重、非音频跳过、命名模板切换、`importlib` 插件发现机制、
冻结宿主 import 白名单、安装器路径与编码、生命周期与残留清理。

**真机验证**向运行中的 Macast 发真实 DLNA SOAP `SetAVTransportURI`，
断言插件在冻结宿主里完成完整链路。

## 故障排查

**托盘菜单里没有「音乐缓存」分组**

1. 确认插件在 Macast 的配置目录（`%LOCALAPPDATA%\xfangfang\Macast\renderer\music_cache.py`）。
2. 看日志 `%LOCALAPPDATA%\xfangfang\Macast\macast.log`。
   插件加载失败只会被 `MacastPlugin.load_from_file` 静默吞掉，日志里会留一条 error。
3. 若用了「设置 → 选择播放器」但显示回退到了 MPV，说明 `Macast_Renderer` 里的名字
   与插件的 `<macast.title>` 不一致。

**投送了但没落盘**

- 缓存目录不可写。
- 投送 URL 已过期（多数音乐 App 的投送链接有时效），日志里会有「缓存失败」。
- 内容是视频且开着「仅缓存音频」。

**文件名是 URL 里的乱码**

音频没有内嵌标签，DIDL 元数据也没带艺术家/歌名，于是回退到 URL 文件名。
可以在「文件名格式」里换模板，或手工改 `Cache_NameFormat`。


## 已知限制

- 投送源是本地文件路径（非 HTTP）时跳过缓存——它本来就在本地。
- 缓存与播放会各占一份下行带宽，这是并行缓存换来的代价。
- 插件设置存在 `macast_setting.json` 里；用 Macast 网页版「高级设置」保存会整体
  覆盖该文件，可能丢掉 `Cache_*` 配置。
- Macast 被强杀（任务管理器结束进程）时正在下载的 `.part` 会留下，下次启动插件会清理。

## 致谢

- [xfangfang/Macast](https://github.com/xfangfang/Macast) —— 本插件所依托的 DLNA 渲染器，
  其插件机制让这一切成为可能。

## 许可

[GPL-3.0-or-later](LICENSE)。

本插件在运行时导入 [Macast](https://github.com/xfangfang/Macast)（`macast.*`、
`macast_renderer.mpv`），而 Macast 以 GPL-3.0 发布，因此本插件属于其衍生作品，
必须以同一许可证发布。各源文件头部带 `SPDX-License-Identifier: GPL-3.0-or-later`
标识；`LICENSE` 为 GPL-3.0 全文，由 Macast 上游原样沿用。

Copyright (C) 2026 KiriChen-Wind
