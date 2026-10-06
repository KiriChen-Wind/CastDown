# -*- coding: utf-8 -*-
# Macast 音乐缓存插件 —— DLNA 投送自动缓存，并按「艺术家-歌名」重命名
# Copyright (C) 2026 KiriChen-Wind
# SPDX-License-Identifier: GPL-3.0-or-later
#
# 本插件在运行时导入 GPL-3.0 的 Macast（macast.*、macast_renderer.mpv），
# 因此同样以 GPL-3.0-or-later 发布。详见仓库根目录 LICENSE。
"""Macast 音乐缓存渲染器插件

当 DLNA 客户端投送媒体到本机时，自动把流下载到用户指定目录，
读取音频内嵌标签，按「艺术家-歌名」重命名，并弹出「缓存完毕」提示。

设计约束（很重要）：
  Macast 官方发行版是 PyInstaller 冻结的，宿主解释器里没有 mutagen 之类的
  第三方库。因此本插件**只使用 Python 标准库**，标签解析为自实现。

<macast.title>自动抓取</macast.title>
<macast.renderer>MusicCacheRenderer</macast.renderer>
<macast.platform>darwin,win32,linux</macast.platform>
<macast.version>1.0.0</macast.version>
<macast.author>KiriChen</macast.author>
<macast.desc>将投送的音乐自动缓存到指定目录</macast.desc>
"""

import os
import re
import sys
import uuid
import time
import gettext
import logging
import threading
import subprocess
import urllib.request
import urllib.parse
import urllib.error
from enum import Enum

import cherrypy

from macast.utils import Setting
from macast.gui import MenuItem
from macast_renderer.mpv import MPVRenderer, MPVRendererSetting

logger = logging.getLogger("MusicCache")
logger.setLevel(logging.INFO)

_ = gettext.gettext

# ---------------------------------------------------------------- 常量

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")

AUDIO_EXT = {
    '.mp3', '.mp2', '.mpga', '.flac', '.m4a', '.m4b', '.aac', '.wav',
    '.ogg', '.oga', '.opus', '.wma', '.ape', '.alac', '.aiff', '.aif',
    '.wv', '.mka', '.dsf', '.dff',
}

CTYPE_EXT = {
    'audio/mpeg': '.mp3',
    'audio/mp3': '.mp3',
    'audio/flac': '.flac',
    'audio/x-flac': '.flac',
    'audio/mp4': '.m4a',
    'audio/x-m4a': '.m4a',
    'audio/aac': '.aac',
    'audio/aacp': '.aac',
    'audio/ogg': '.ogg',
    'application/ogg': '.ogg',
    'audio/opus': '.opus',
    'audio/wav': '.wav',
    'audio/x-wav': '.wav',
    'audio/x-ms-wma': '.wma',
}

MIN_BYTES = 1024
TMP_DIRNAME = '.macast_cache_tmp'

# .m4a/.mka 无法仅凭文件头与视频容器区分，需依赖 Content-Type 或 URL 后缀确认
UNAMBIGUOUS_AUDIO_EXT = {
    '.mp3', '.mp2', '.mpga', '.flac', '.aac', '.wav', '.ogg', '.oga',
    '.opus', '.wma', '.ape', '.alac', '.aiff', '.aif', '.wv', '.dsf', '.dff',
}
VIDEO_EXT = {
    '.mp4', '.m4v', '.mkv', '.webm', '.avi', '.mov', '.flv', '.ts',
    '.wmv', '.mpg', '.mpeg', '.rmvb', '.3gp', '.rm',
}


class CacheSetting(Enum):
    """插件自己的设置项。Setting 以 property.name 作为存储键，
    因此这里用带前缀的名字避免与 Macast 内置项冲突。"""
    Cache_Enable = 1
    Cache_Dir = 2
    Cache_AudioOnly = 3
    Cache_Toast = 4
    Cache_NameFormat = 5
    Cache_UserAgent = 6
    Cache_MaxSizeMB = 7


# ================================================================ 标签解析
# 以下为纯标准库实现：ID3v2 / ID3v1 / FLAC / MP4(M4A) / Ogg / WAV

def _syncsafe(b):
    if len(b) != 4:
        return 0
    return ((b[0] & 0x7F) << 21) | ((b[1] & 0x7F) << 14) | ((b[2] & 0x7F) << 7) | (b[3] & 0x7F)


def _decode_id3_text(payload):
    """ID3 文本帧：首字节为编码标记。"""
    if not payload:
        return ''
    enc, body = payload[0], payload[1:]
    try:
        if enc == 0:
            txt = body.decode('latin-1')
        elif enc == 1:
            txt = body.decode('utf-16') if body[:2] in (b'\xff\xfe', b'\xfe\xff') \
                else body.decode('utf-16-le')
        elif enc == 2:
            txt = body.decode('utf-16-be')
        else:
            txt = body.decode('utf-8', 'replace')
    except Exception:
        return ''
    return txt.replace('\x00', '').strip()


def _read_id3v2(f):
    f.seek(0)
    hdr = f.read(10)
    if len(hdr) < 10 or hdr[:3] != b'ID3':
        return {}
    major, flags, size = hdr[3], hdr[5], _syncsafe(hdr[6:10])
    data = f.read(size)
    pos = 0
    if flags & 0x40 and major >= 3:
        # 扩展头：v2.4 长度为 syncsafe，v2.3 为普通 4 字节
        if major == 4:
            pos += _syncsafe(data[0:4])
        else:
            pos += 4 + int.from_bytes(data[0:4], 'big')
    idlen, szlen, extra = (3, 3, 0) if major == 2 else (4, 4, 2)
    frames = {}
    while pos + idlen + szlen + extra <= len(data):
        fid = data[pos:pos + idlen]
        if not fid or fid[0] == 0:
            break
        raw_size = data[pos + idlen:pos + idlen + szlen]
        fsz = _syncsafe(raw_size) if major == 4 else int.from_bytes(raw_size, 'big')
        pos += idlen + szlen + extra
        if fsz <= 0 or pos + fsz > len(data):
            break
        frames[fid] = data[pos:pos + fsz]
        pos += fsz

    def pick(*ids):
        for i in ids:
            if frames.get(i):
                v = _decode_id3_text(frames[i])
                if v:
                    return v
        return ''

    return {
        'artist': pick(b'TPE1', b'TP1'),
        'title': pick(b'TIT2', b'TT2'),
        'album': pick(b'TALB', b'TAL'),
    }


def _read_id3v1(f):
    f.seek(0, os.SEEK_END)
    size = f.tell()
    if size < 128:
        return {}
    f.seek(size - 128)
    tag = f.read(128)
    if tag[:3] != b'TAG':
        return {}

    def field(a, b):
        return tag[a:b].split(b'\x00')[0].decode('latin-1', 'replace').strip()

    return {'title': field(3, 33), 'artist': field(33, 63), 'album': field(63, 93)}


def _parse_vorbis_comment(data):
    """Vorbis comment / OpusTags 公共结构。"""
    res = {}
    try:
        if len(data) < 8:
            return res
        vendor_len = int.from_bytes(data[0:4], 'little')
        pos = 4 + vendor_len
        if pos + 4 > len(data):
            return res
        count = int.from_bytes(data[pos:pos + 4], 'little')
        pos += 4
        for _i in range(min(count, 4096)):
            if pos + 4 > len(data):
                break
            length = int.from_bytes(data[pos:pos + 4], 'little')
            pos += 4
            item = data[pos:pos + length].decode('utf-8', 'replace')
            pos += length
            if '=' in item:
                k, v = item.split('=', 1)
                res.setdefault(k.strip().upper(), v.strip())
    except Exception:
        pass
    return {
        'artist': res.get('ARTIST', '') or res.get('ALBUMARTIST', ''),
        'title': res.get('TITLE', ''),
        'album': res.get('ALBUM', ''),
    }


def _read_flac(f):
    f.seek(0)
    if f.read(4) != b'fLaC':
        return {}
    for _i in range(128):
        hdr = f.read(4)
        if len(hdr) < 4:
            break
        btype = hdr[0] & 0x7F
        blen = int.from_bytes(hdr[1:4], 'big')
        if btype == 4:  # VORBIS_COMMENT
            return _parse_vorbis_comment(f.read(blen))
        if btype == 127:
            break
        f.seek(blen, os.SEEK_CUR)
        if hdr[0] & 0x80:
            break
    return {}


def _read_ogg(f):
    f.seek(0)
    head = f.read(1 << 21)  # 注释块总在前部，2MB 足够
    idx = head.find(b'OpusTags')
    if idx >= 0:
        return _parse_vorbis_comment(head[idx + 8:])
    idx = head.find(b'\x03vorbis')
    if idx >= 0:
        return _parse_vorbis_comment(head[idx + 7:])
    return {}


def _iter_boxes(f, start, end):
    """遍历 MP4 box（基于 seek，避免整文件读入内存）。"""
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        hdr = f.read(8)
        if len(hdr) < 8:
            return
        size = int.from_bytes(hdr[0:4], 'big')
        typ = hdr[4:8]
        hsz = 8
        if size == 1:
            ext = f.read(8)
            if len(ext) < 8:
                return
            size = int.from_bytes(ext, 'big')
            hsz = 16
        elif size == 0:
            size = end - pos
        if size < hsz:
            return
        yield typ, pos + hsz, pos + size
        pos += size


def _read_mp4(f):
    f.seek(0, os.SEEK_END)
    total = f.tell()
    ilst = None
    for typ, s, e in _iter_boxes(f, 0, total):
        if typ != b'moov':
            continue
        for t2, s2, e2 in _iter_boxes(f, s, e):
            if t2 != b'udta':
                continue
            for t3, s3, e3 in _iter_boxes(f, s2, e2):
                if t3 != b'meta':
                    continue
                for t4, s4, e4 in _iter_boxes(f, s3 + 4, e3):  # meta 为 FullBox
                    if t4 == b'ilst':
                        ilst = (s4, e4)
    if not ilst:
        return {}
    want = {b'\xa9ART': 'artist', b'\xa9nam': 'title', b'\xa9alb': 'album',
            b'aART': 'album_artist'}
    res = {}
    for typ, s2, e2 in _iter_boxes(f, ilst[0], ilst[1]):
        key = want.get(typ)
        if not key:
            continue
        for t2, s3, e3 in _iter_boxes(f, s2, e2):
            if t2 != b'data':
                continue
            f.seek(s3)
            payload = f.read(min(e3 - s3, 1 << 20))
            if len(payload) <= 8:
                break
            res[key] = payload[8:].decode('utf-8', 'replace').replace('\x00', '').strip()
            break
    if not res.get('artist') and res.get('album_artist'):
        res['artist'] = res['album_artist']
    return res


def _read_riff(f):
    res = {}
    f.seek(12)
    for _i in range(64):
        hdr = f.read(8)
        if len(hdr) < 8:
            break
        cid = hdr[0:4]
        size = int.from_bytes(hdr[4:8], 'little')
        if cid == b'LIST':
            sub = f.read(min(size, 1 << 20))
            for tag, key in ((b'IART', 'artist'), (b'INAM', 'title'), (b'IPRD', 'album')):
                i = sub.find(tag)
                if i < 0:
                    continue
                ln = int.from_bytes(sub[i + 4:i + 8], 'little')
                res[key] = sub[i + 8:i + 8 + ln].decode('utf-8', 'replace') \
                    .replace('\x00', '').strip()
            break
        f.seek(size + (size & 1), os.SEEK_CUR)
    return res


def read_audio_tags(path):
    """从音频文件读取 artist/title/album。全部失败时返回空字典。"""
    tags = {}
    try:
        with open(path, 'rb') as f:
            magic = f.read(12)
            f.seek(0)
            if magic[:3] == b'ID3':
                tags = _read_id3v2(f)
            elif magic[:4] == b'fLaC':
                tags = _read_flac(f)
            elif magic[:4] == b'OggS':
                tags = _read_ogg(f)
            elif magic[4:8] == b'ftyp':
                tags = _read_mp4(f)
            elif magic[:4] == b'RIFF':
                tags = _read_riff(f)
            if not tags.get('artist') or not tags.get('title'):
                v1 = _read_id3v1(f)
                for k in ('artist', 'title', 'album'):
                    if not tags.get(k) and v1.get(k):
                        tags[k] = v1[k]
    except Exception as e:
        logger.debug("读取音频标签失败：%s", e)
    return {k: v for k, v in tags.items() if v}


# DIDL-Lite 标签匹配：允许任意命名空间前缀（upnp:artist / dc:title / 无前缀）
_DIDL_TAG = re.compile(
    r'<(?:[A-Za-z0-9_.\-]+:)?(artist|artistName|creator|albumArtist|albumartist'
    r'|title|album|genre)\b[^>]*>(.*?)</(?:[A-Za-z0-9_.\-]+:)?\1\s*>',
    re.IGNORECASE | re.DOTALL)

_ENT = re.compile(r'&(amp|lt|gt|quot|apos|#[0-9]+|#[xX][0-9A-Fa-f]+);')


def _unescape(s):
    """最小化 XML 实体反转义，避免依赖 xml.sax / html 模块。"""
    def rep(m):
        t = m.group(1)
        if t == 'amp':
            return '&'
        if t == 'lt':
            return '<'
        if t == 'gt':
            return '>'
        if t == 'quot':
            return '"'
        if t == 'apos':
            return "'"
        try:
            if t[1] in 'xX':
                return chr(int(t[2:], 16))
            return chr(int(t[1:]))
        except Exception:
            return m.group(0)
    return _ENT.sub(rep, s)


def parse_didl(xml_text):
    """解析 DLNA 投送时携带的 DIDL-Lite 元数据，作为内嵌标签的兜底。

    刻意只用 re，不引入任何 XML 库：官方 Macast 是 PyInstaller 冻结包，
    宿主的 Python 里没有 xml.etree（实测 ModuleNotFoundError），
    顶层导入失败会让整个插件模块加载失败、被 Macast 静默丢弃。
    """
    res = {}
    if not xml_text or not isinstance(xml_text, str):
        return res

    found = {}
    try:
        for m in _DIDL_TAG.finditer(xml_text):
            local = m.group(1).lower()
            val = _unescape(m.group(2)).strip()
            if val:
                found.setdefault(local, val)
    except Exception as e:
        logger.debug("解析 DIDL 失败：%s", e)

    def pick(*names):
        for n in names:
            if found.get(n):
                return found[n]
        return ''

    artist = pick('artist', 'artistname', 'creator')
    album_artist = pick('albumartist')
    if not artist and album_artist:
        artist = album_artist
    if artist:
        res['artist'] = artist
    title = pick('title')
    if title:
        res['title'] = title
    album = pick('album')
    if album:
        res['album'] = album
    if album_artist:
        res['album_artist'] = album_artist
    return res


# ================================================================ 工具函数

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {'CON', 'PRN', 'AUX', 'NUL',
             'COM1', 'COM2', 'COM3', 'COM4', 'COM5', 'COM6', 'COM7', 'COM8', 'COM9',
             'LPT1', 'LPT2', 'LPT3', 'LPT4', 'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9'}


def sanitize(name, maxlen=120):
    """清洗成跨平台安全文件名。"""
    if not name:
        return ''
    name = _ILLEGAL.sub('_', str(name))
    name = re.sub(r'\s+', ' ', name).strip().rstrip('. ')
    if not name:
        return ''
    if name.split('.')[0].upper() in _RESERVED:
        name = '_' + name
    if len(name) > maxlen:
        name = name[:maxlen].rstrip('. ')
    return name


def unique_path(directory, stem, ext):
    """同目录同名时追加 (1) (2)…，绝不覆盖已有文件。"""
    path = os.path.join(directory, stem + ext)
    i = 1
    while os.path.exists(path):
        path = os.path.join(directory, '{} ({}){}'.format(stem, i, ext))
        i += 1
    return path


def url_ext(url):
    try:
        path = urllib.parse.urlparse(url).path
    except Exception:
        return ''
    ext = os.path.splitext(path)[1].lower()
    return ext if ext in AUDIO_EXT else ''


def url_stem(url):
    try:
        path = urllib.parse.urlparse(url).path
    except Exception:
        return ''
    base = os.path.basename(path)
    return os.path.splitext(base)[0]


def sniff_ext(path):
    """按文件头判断真实容器，优先于 URL 后缀。"""
    try:
        with open(path, 'rb') as f:
            head = f.read(16)
    except Exception:
        return ''
    if head[:3] == b'ID3':
        return '.mp3'
    if head[:4] == b'fLaC':
        return '.flac'
    if head[:4] == b'OggS':
        return '.ogg'
    if head[:4] == b'RIFF':
        return '.wav'
    if head[4:8] == b'ftyp':
        return '.m4a'
    if head[:4] == b'\x1a\x45\xdf\xa3':
        return '.mka'
    if head[:4] == b'MAC ':
        return '.ape'
    if head[:4] == b'DSD ':
        return '.dsf'
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        # layer 位为 0 => ADTS AAC，否则 MPEG 音频
        return '.aac' if (head[1] & 0x06) == 0x00 else '.mp3'
    return ''


def default_cache_dir():
    return os.path.join(os.path.expanduser('~'), 'Music', 'MacastCache')


def open_directory(path):
    try:
        if not os.path.isdir(path):
            os.makedirs(path, exist_ok=True)
        if sys.platform == 'darwin':
            subprocess.Popen(['open', path])
        elif sys.platform == 'win32':
            subprocess.Popen(['explorer.exe', os.path.normpath(path)])
        else:
            subprocess.Popen(['xdg-open', path])
        return True
    except Exception as e:
        logger.error("打开目录失败：%s", e)
        return False


def choose_directory(prompt='选择音乐缓存目录'):
    """跨平台原生目录选择对话框；取消或失败返回空串。"""
    try:
        if sys.platform == 'win32':
            script = (
                'Add-Type -AssemblyName System.Windows.Forms;'
                '$d=New-Object System.Windows.Forms.FolderBrowserDialog;'
                '$d.Description="{}";'
                '$d.ShowNewFolderButton=$true;'
                'if($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK)'
                '{{[Console]::Out.Write($d.SelectedPath)}}'
            ).format(prompt)
            proc = subprocess.run(
                ['powershell', '-NoProfile', '-STA', '-Command', script],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            out = proc.stdout.decode('utf-8', 'replace').strip()
            return out if out and os.path.isdir(out) else ''
        if sys.platform == 'darwin':
            proc = subprocess.run(
                ['osascript', '-e',
                 'POSIX path of (choose folder with prompt "{}")'.format(prompt)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            out = proc.stdout.decode('utf-8', 'replace').strip()
            return out if out and os.path.isdir(out) else ''
        for cmd in (['zenity', '--file-selection', '--directory', '--title', prompt],
                    ['kdialog', '--getexistingdirectory', os.path.expanduser('~')]):
            try:
                proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            except FileNotFoundError:
                continue
            out = proc.stdout.decode('utf-8', 'replace').strip()
            if out and os.path.isdir(out):
                return out
    except Exception as e:
        logger.error("选择目录失败：%s", e)
    return ''


# ================================================================ 渲染器

class MusicCacheRenderer(MPVRenderer):
    """在 MPVRenderer 基础上拦截投送 URL，后台并行缓存。"""

    def __init__(self):
        super(MusicCacheRenderer, self).__init__(
            gettext.gettext, Setting.mpv_default_path)
        self.renderer_setting = MusicCacheRendererSetting()
        self._lock = threading.Lock()
        self._jobs = {}          # url -> Thread
        self._done = {}          # url -> 落盘路径（会话内去重）
        self._stopping = False
        self._tmp_cleaned = False

    # -------- 生命周期

    def start(self):
        # reload() 会走 stop() -> start()，必须复位停止标志，
        # 否则用户改一次播放器设置之后缓存就永远不再工作（且不报错）。
        self._stopping = False
        super(MusicCacheRenderer, self).start()
        if not self._tmp_cleaned:
            # 只在首次启动清理：此时不可能有下载在途。
            # reload 时不动，免得删掉正在写入的 .part。
            self._tmp_cleaned = True
            self._clean_stale_tmp()

    def stop(self):
        self._stopping = True
        super(MusicCacheRenderer, self).stop()

    def _clean_stale_tmp(self):
        """清理上次被强杀（SIGKILL / 任务管理器结束进程）时残留的 .part。"""
        try:
            cache_dir = Setting.get(CacheSetting.Cache_Dir, default_cache_dir())
            tmp_dir = os.path.join(cache_dir, TMP_DIRNAME)
            if not os.path.isdir(tmp_dir):
                return
            for name in os.listdir(tmp_dir):
                if not name.endswith('.part'):
                    continue
                path = os.path.join(tmp_dir, name)
                try:
                    os.remove(path)
                    logger.info("已清理上次残留的临时文件：%s", path)
                except OSError as e:
                    logger.debug("清理临时文件失败 %s：%s", path, e)
        except Exception as e:
            logger.debug("扫描临时目录失败：%s", e)

    # -------- 投送入口

    def set_media_url(self, url, start="0"):
        # 先播放，缓存完全异步，绝不阻塞投送
        super(MusicCacheRenderer, self).set_media_url(url, start)
        try:
            self._schedule(url)
        except Exception as e:
            logger.error("启动缓存任务失败：%s", e)

    def set_media_title(self, data):
        super(MusicCacheRenderer, self).set_media_title(data)
        self._dlna_title = data

    # -------- 调度

    def _schedule(self, url):
        if self._stopping or not url:
            return
        if not Setting.get(CacheSetting.Cache_Enable, 1):
            return
        if url.startswith('file://') or url.startswith('/'):
            return  # 本地文件无需缓存
        with self._lock:
            if url in self._done or url in self._jobs:
                return
            t = threading.Thread(target=self._cache_job, args=(url,),
                                 daemon=True, name="MusicCache")
            self._jobs[url] = t
        t.start()
        logger.info("已排入缓存队列：%s", url)

    def _finish(self, url):
        with self._lock:
            self._jobs.pop(url, None)

    # -------- 缓存主体

    def _cache_job(self, url):
        tmp = None
        try:
            cache_dir = self._ensure_dir()
            tmp_dir = os.path.join(cache_dir, TMP_DIRNAME)
            os.makedirs(tmp_dir, exist_ok=True)
            tmp = os.path.join(tmp_dir, uuid.uuid4().hex + '.part')

            headers = {
                'User-Agent': Setting.get(CacheSetting.Cache_UserAgent, DEFAULT_UA),
                'Accept': '*/*',
                'Connection': 'close',
            }
            size, ctype = self._download(url, tmp, headers)
            if size < MIN_BYTES:
                raise ValueError('下载内容过小({}B)，判定为无效流'.format(size))

            if Setting.get(CacheSetting.Cache_AudioOnly, 1) and \
                    not self._is_audio(url, ctype, tmp):
                logger.info("跳过非音频内容：%s（%s）", url, ctype)
                return

            tags = read_audio_tags(tmp)
            didl = parse_didl(self._current_metadata())
            artist = tags.get('artist') or didl.get('artist') or ''
            title = tags.get('title') or didl.get('title') or ''
            album = tags.get('album') or didl.get('album') or ''

            ext = (sniff_ext(tmp) or url_ext(url)
                   or CTYPE_EXT.get((ctype or '').split(';')[0].strip().lower(), ''))
            stem = self._build_stem(artist, title, album, url)
            dest = unique_path(cache_dir, stem, ext)

            os.replace(tmp, dest)
            tmp = None
            with self._lock:
                self._done[url] = dest

            logger.info("缓存完成：%s -> %s（%.1fMB）", url, dest, size / 1048576.0)
            self._notify_done(dest)
        except InterruptedError:
            logger.info("缓存被中断：%s", url)
        except Exception as e:
            logger.error("缓存失败 %s：%s", url, e)
            cherrypy.engine.publish('app_notify', _('音乐缓存失败'), str(e))
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            self._finish(url)

    def _ensure_dir(self):
        cache_dir = Setting.get(CacheSetting.Cache_Dir, default_cache_dir())
        if not cache_dir:
            cache_dir = default_cache_dir()
        os.makedirs(cache_dir, exist_ok=True)
        return cache_dir

    def _download(self, url, dest, headers, chunk=1 << 16):
        max_bytes = int(Setting.get(CacheSetting.Cache_MaxSizeMB, 512)) * 1048576
        req = urllib.request.Request(url, headers=headers)
        total = 0
        ctype = ''
        with urllib.request.urlopen(req, timeout=30) as resp:
            ctype = resp.headers.get('Content-Type', '') or ''
            with open(dest, 'wb') as out:
                while True:
                    if self._stopping:
                        raise InterruptedError('renderer stopped')
                    data = resp.read(chunk)
                    if not data:
                        break
                    out.write(data)
                    total += len(data)
                    if max_bytes and total > max_bytes:
                        raise ValueError('超过单文件上限 {}MB'.format(max_bytes // 1048576))
        return total, ctype

    @staticmethod
    def _is_audio(url, ctype, path):
        main = (ctype or '').split(';')[0].strip().lower()
        ext = url_ext(url)
        if main.startswith('video/') or ext in VIDEO_EXT:
            return False
        if main.startswith('audio/') or main in ('application/ogg', 'application/x-ogg'):
            return True
        if ext in UNAMBIGUOUS_AUDIO_EXT:
            return True
        return sniff_ext(path) in UNAMBIGUOUS_AUDIO_EXT

    def _current_metadata(self):
        """等待并读取 DLNA 投送的 DIDL-Lite（SetAVTransportURI 会写该状态）。"""
        for _i in range(20):
            try:
                xml = self.protocol.get_state('CurrentTrackMetaData')
            except Exception:
                xml = ''
            if xml:
                return xml
            if self._stopping:
                return ''
            time.sleep(0.1)
        return ''

    @staticmethod
    def _build_stem(artist, title, album, url):
        fmt = Setting.get(CacheSetting.Cache_NameFormat, '{artist}-{title}')
        artist = sanitize(artist, 60)
        title = sanitize(title, 80)
        album = sanitize(album, 60)
        fallback = sanitize(url_stem(url), 80)
        if not title:
            title = fallback
        if not artist and not title:
            title = '未知歌曲'
        stem = (fmt.replace('{artist}', artist or '未知艺术家')
                   .replace('{title}', title or '未知歌曲')
                   .replace('{album}', album))
        stem = sanitize(stem)
        return stem or '未命名'

    def _notify_done(self, dest):
        name = os.path.basename(dest)
        cherrypy.engine.publish('app_notify', _('缓存完毕'), name)
        if Setting.get(CacheSetting.Cache_Toast, 1):
            try:
                self.set_media_text(_('缓存完毕') + '：' + name, 3000)
            except Exception:
                pass


# ================================================================ 菜单设置

class MusicCacheRendererSetting(MPVRendererSetting):
    """在 MPV 播放器设置之后追加「音乐缓存」设置组。"""

    def __init__(self):
        super(MusicCacheRendererSetting, self).__init__()
        self.enable_item = None
        self.audio_only_item = None
        self.toast_item = None
        self.name_format_item = None
        self.dir_item = None

    def build_menu(self):
        items = super(MusicCacheRendererSetting, self).build_menu()

        self.enable_item = MenuItem(
            _('启用音乐缓存'), self.on_enable_toggle,
            checked=bool(Setting.get(CacheSetting.Cache_Enable, 1)))
        self.audio_only_item = MenuItem(
            _('仅缓存音频'), self.on_audio_only_toggle,
            checked=bool(Setting.get(CacheSetting.Cache_AudioOnly, 1)))
        self.toast_item = MenuItem(
            _('播放器内提示'), self.on_toast_toggle,
            checked=bool(Setting.get(CacheSetting.Cache_Toast, 1)))

        current_fmt = Setting.get(CacheSetting.Cache_NameFormat, '{artist}-{title}')
        self.name_format_item = MenuItem(
            _('文件名格式'), children=[
                MenuItem(_('艺术家-歌名'), self.on_format_click, data=0),
                MenuItem(_('艺术家 - 歌名'), self.on_format_click, data=1),
                MenuItem(_('歌名-艺术家'), self.on_format_click, data=2),
            ])
        for i, fmt in enumerate(('{artist}-{title}', '{artist} - {title}', '{title}-{artist}')):
            if current_fmt == fmt:
                self.name_format_item.items()[i].checked = True

        cache_dir = Setting.get(CacheSetting.Cache_Dir, default_cache_dir())
        self.dir_item = MenuItem(_('缓存目录'), children=[
            MenuItem(_('选择目录…'), self.on_choose_dir),
            MenuItem(_('打开缓存目录'), self.on_open_dir),
            MenuItem(_('恢复默认目录'), self.on_reset_dir),
            MenuItem(cache_dir, enabled=False),
        ])

        cache_menu = [
            MenuItem(_('音乐缓存'), enabled=False),
            self.enable_item,
            self.audio_only_item,
            self.toast_item,
            self.name_format_item,
            self.dir_item,
        ]
        return items + [None] + cache_menu

    # -------- 回调

    def _refresh(self):
        cherrypy.engine.publish('app_notify',
                                _('音乐缓存'),
                                _('设置已保存'), sound=False)

    def on_enable_toggle(self, item):
        item.checked = not item.checked
        Setting.set(CacheSetting.Cache_Enable, 1 if item.checked else 0)
        self._refresh()

    def on_audio_only_toggle(self, item):
        item.checked = not item.checked
        Setting.set(CacheSetting.Cache_AudioOnly, 1 if item.checked else 0)
        self._refresh()

    def on_toast_toggle(self, item):
        item.checked = not item.checked
        Setting.set(CacheSetting.Cache_Toast, 1 if item.checked else 0)
        self._refresh()

    def on_format_click(self, item):
        for i in self.name_format_item.items():
            i.checked = False
        item.checked = True
        fmt = ('{artist}-{title}', '{artist} - {title}', '{title}-{artist}')[item.data]
        Setting.set(CacheSetting.Cache_NameFormat, fmt)
        self._refresh()

    def on_choose_dir(self, item):
        def work():
            path = choose_directory()
            if path:
                Setting.set(CacheSetting.Cache_Dir, path)
                if self.dir_item is not None:
                    self.dir_item.items()[3].text = path
                cherrypy.engine.publish('app_notify', _('音乐缓存'),
                                        _('缓存目录：') + path)
        threading.Thread(target=work, daemon=True, name="MusicCacheChooseDir").start()

    def on_open_dir(self, item):
        open_directory(Setting.get(CacheSetting.Cache_Dir, default_cache_dir()))

    def on_reset_dir(self, item):
        path = default_cache_dir()
        Setting.set(CacheSetting.Cache_Dir, path)
        if self.dir_item is not None:
            self.dir_item.items()[3].text = path
        self._refresh()
