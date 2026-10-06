# -*- coding: utf-8 -*-
# Macast 音乐缓存插件 —— 离线验证夹具
# Copyright (C) 2026 KiriChen-Wind
# SPDX-License-Identifier: GPL-3.0-or-later
"""music_cache.py 离线验证夹具。

Macast 宿主（cherrypy / macast.* / macast_renderer.mpv）在测试环境不可用，
这里用等价桩模块替换，从而对插件本体做真实的端到端验证：
  1) 纯标准库标签解析（ID3v2.3 / ID3v1 / FLAC / MP4-M4A / Ogg-Opus / WAV）
  2) 文件名清洗、命名模板、去重
  3) 起本地 HTTP 服务 -> set_media_url -> 断言落盘命名与「缓存完毕」通知
  4) 非音频内容按配置跳过
  5) DIDL-Lite 兜底命名
"""

import io
import os
import re
import sys
import ast
import json
import time
import shutil
import types
import tempfile
import threading
import http.server
import importlib
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(os.path.dirname(HERE), 'renderer', 'music_cache.py')

FAILURES = []
CHECKS = 0


def check(cond, label, extra=''):
    global CHECKS
    CHECKS += 1
    if cond:
        print('  [通过] {}'.format(label))
    else:
        print('  [失败] {} {}'.format(label, extra))
        FAILURES.append(label)


# ----------------------------------------------------------- 桩模块

class _Setting:
    setting = {}
    mpv_default_path = 'mpv'

    @staticmethod
    def load():
        pass

    @staticmethod
    def get(prop, default=1):
        return _Setting.setting.get(prop.name, default)

    @staticmethod
    def set(prop, value):
        _Setting.setting[prop.name] = value


NOTIFICATIONS = []


class _StubRendererBase:
    def __init__(self, lang=None, path='mpv'):
        self.path = path
        self.protocol = _StubProtocol()
        self.last_url = None
        self.toasts = []
        self.running = False

    def start(self):
        self.running = True

    def set_media_url(self, url, start="0"):
        self.last_url = url

    def set_media_title(self, data):
        pass

    def set_media_text(self, data, duration=1000):
        self.toasts.append(data)

    def stop(self):
        pass


class _StubProtocol:
    metadata = ''

    def get_state(self, name):
        if name == 'CurrentTrackMetaData':
            return _StubProtocol.metadata
        return ''


class _StubRendererSetting:
    def build_menu(self):
        return []


class _StubMenuItem:
    def __init__(self, text, callback=None, checked=None, enabled=True,
                 children=None, data=None, key=None):
        self.text = text
        self.callback = callback
        self.checked = checked
        self.enabled = enabled
        self.children = children
        self.data = data
        self.id = text

    def items(self):
        return self.children or []


def install_stubs():
    cherrypy = types.ModuleType('cherrypy')
    cherrypy.engine = types.SimpleNamespace(
        publish=lambda *a, **k: NOTIFICATIONS.append(a))
    sys.modules['cherrypy'] = cherrypy

    macast = types.ModuleType('macast')
    macast.__path__ = []
    macast_utils = types.ModuleType('macast.utils')
    macast_utils.Setting = _Setting
    macast_gui = types.ModuleType('macast.gui')
    macast_gui.MenuItem = _StubMenuItem
    sys.modules['macast'] = macast
    sys.modules['macast.utils'] = macast_utils
    sys.modules['macast.gui'] = macast_gui

    mr = types.ModuleType('macast_renderer')
    mr.__path__ = []
    mr_mpv = types.ModuleType('macast_renderer.mpv')
    mr_mpv.MPVRenderer = _StubRendererBase
    mr_mpv.MPVRendererSetting = _StubRendererSetting
    sys.modules['macast_renderer'] = mr
    sys.modules['macast_renderer.mpv'] = mr_mpv

    spec = importlib.util.spec_from_file_location('music_cache', PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules['music_cache'] = mod
    spec.loader.exec_module(mod)
    return mod


# ----------------------------------------------------------- 音频合成

def _id3v2_frame(fid, text):
    payload = b'\x03' + text.encode('utf-8')
    return fid + len(payload).to_bytes(4, 'big') + b'\x00\x00' + payload


def make_mp3_id3v2(artist, title, album):
    frames = (_id3v2_frame(b'TIT2', title)
              + _id3v2_frame(b'TPE1', artist)
              + _id3v2_frame(b'TALB', album))
    n = len(frames)
    syncsafe = bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])
    return b'ID3\x03\x00\x00' + syncsafe + frames + b'\xff\xfb\x90\x00' + b'\x00' * 70000


def make_mp3_id3v1(artist, title, album):
    def f(s, n):
        b = s.encode('latin-1')[:n]
        return b + b'\x00' * (n - len(b))
    tag = (b'TAG' + f(title, 30) + f(artist, 30) + f(album, 30)
           + f('2024', 4) + f('', 30) + b'\x00')
    assert len(tag) == 128
    return b'\xff\xfb\x90\x00' + b'\x00' * 70000 + tag


def _vorbis_comment(d):
    vendor = b'reference-libFLAC'
    out = len(vendor).to_bytes(4, 'little') + vendor
    out += len(d).to_bytes(4, 'little')
    for k, v in d.items():
        item = ('{}={}'.format(k, v)).encode('utf-8')
        out += len(item).to_bytes(4, 'little') + item
    return out


def make_flac(artist, title, album):
    si = bytes([0x00]) + (34).to_bytes(3, 'big') + b'\x00' * 34
    vc = _vorbis_comment({'ARTIST': artist, 'TITLE': title, 'ALBUM': album})
    block = bytes([0x84]) + len(vc).to_bytes(3, 'big') + vc
    return b'fLaC' + si + block


def make_ogg(artist, title, album):
    vc = _vorbis_comment({'ARTIST': artist, 'TITLE': title, 'ALBUM': album})
    return (b'OggS\x00\x02' + b'\x00' * 200 + b'OpusHead\x01\x02'
            + b'\x00' * 100 + b'OpusTags' + vc + b'\x00' * 64)


def _box(typ, payload):
    return (len(payload) + 8).to_bytes(4, 'big') + typ + payload


def make_m4a(artist, title):
    def data_box(val):
        return _box(b'data', (1).to_bytes(4, 'big') + b'\x00' * 4 + val.encode('utf-8'))
    ilst = _box(b'ilst', _box(b'\xa9ART', data_box(artist)) + _box(b'\xa9nam', data_box(title)))
    meta = _box(b'meta', b'\x00\x00\x00\x00' + ilst)
    moov = _box(b'moov', _box(b'udta', meta))
    ftyp = _box(b'ftyp', b'M4A \x00\x00\x00\x00M4A mp42')
    # moov 故意放在文件尾部，验证 seek 式定位
    return ftyp + _box(b'mdat', b'\x00' * 5000) + moov


def make_wav(artist, title):
    def chunk(cid, s):
        return cid + len(s).to_bytes(4, 'little') + s + (b'\x00' if len(s) % 2 else b'')
    info = chunk(b'IART', artist.encode()) + chunk(b'INAM', title.encode())
    lst = chunk(b'LIST', b'INFO' + info)
    fmt = chunk(b'fmt ', b'\x00' * 16)
    data = chunk(b'data', b'\x00' * 2000)
    body = b'WAVE' + fmt + lst + data
    return b'RIFF' + len(body).to_bytes(4, 'little') + body


# ----------------------------------------------------------- 本地 HTTP 服务

class Handler(http.server.BaseHTTPRequestHandler):
    payload = b''
    ctype = 'audio/mpeg'

    def do_GET(self):
        if self.path.startswith('/video.mp4'):
            body = b'\x00\x00\x00\x18ftypmp42' + b'\x00' * 5000
            self.send_response(200)
            self.send_header('Content-Type', 'video/mp4')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = Handler.payload
        self.send_response(200)
        self.send_header('Content-Type', Handler.ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def start_server():
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, 'http://127.0.0.1:{}'.format(srv.server_address[1])


def wait_file(directory, timeout=25):
    end = time.time() + timeout
    while time.time() < end:
        if os.path.isdir(directory):
            names = [n for n in os.listdir(directory) if not n.startswith('.')]
            if names:
                return names[0]
        time.sleep(0.2)
    return ''


# ----------------------------------------------------------- 用例

def main():
    mod = install_stubs()
    tmp = tempfile.mkdtemp(prefix='musiccache_test_')

    print('\n== 1. 音频标签解析 ==')
    cases = [
        ('id3v2.3 mp3', make_mp3_id3v2('周杰伦', '晴天', '叶惠美'), '周杰伦', '晴天', '叶惠美'),
        ('id3v1 mp3', make_mp3_id3v1('Adele', 'Hello', '25'), 'Adele', 'Hello', '25'),
        ('flac', make_flac('Radiohead', 'Creep', 'Pablo Honey'), 'Radiohead', 'Creep', 'Pablo Honey'),
        ('m4a', make_m4a('Aimer', 'Ref:rain'), 'Aimer', 'Ref:rain', ''),
        ('ogg opus', make_ogg('YOASOBI', '夜に駆ける', 'THE BOOK'), 'YOASOBI', '夜に駆ける', 'THE BOOK'),
        ('wav', make_wav('Daft Punk', 'Get Lucky'), 'Daft Punk', 'Get Lucky', ''),
    ]
    for label, blob, artist, title, album in cases:
        p = os.path.join(tmp, label.replace(' ', '_') + '.bin')
        with open(p, 'wb') as f:
            f.write(blob)
        tags = mod.read_audio_tags(p)
        check(tags.get('artist') == artist, '{} 艺术家={!r}'.format(label, tags.get('artist')))
        check(tags.get('title') == title, '{} 标题={!r}'.format(label, tags.get('title')))
        if album:
            check(tags.get('album') == album, '{} 专辑={!r}'.format(label, tags.get('album')))

    print('\n== 2. 容器嗅探 ==')
    p = os.path.join(tmp, 'sniff_mp3')
    open(p, 'wb').write(make_mp3_id3v2('x', 'y', 'z'))
    check(mod.sniff_ext(p) == '.mp3', 'mp3 嗅探')
    p = os.path.join(tmp, 'sniff_m4a')
    open(p, 'wb').write(make_m4a('x', 'y'))
    check(mod.sniff_ext(p) == '.m4a', 'm4a 嗅探')
    p = os.path.join(tmp, 'sniff_aac')
    open(p, 'wb').write(b'\xff\xf1\x50\x80' + b'\x00' * 100)
    check(mod.sniff_ext(p) == '.aac', 'adts aac 嗅探')

    print('\n== 3. 文件名清洗与去重 ==')
    check(mod.sanitize('a/b\\c:d*e?f"g<h>i|j') == 'a_b_c_d_e_f_g_h_i_j', '非法字符替换')
    check(mod.sanitize('  trailing.  ') == 'trailing', '尾部点/空格清理')
    check(mod.sanitize('CON') == '_CON', 'Windows 保留名')
    check(mod.sanitize('x' * 300).__len__() == 120, '长度上限')
    check(mod.sanitize('') == '', '空串')
    d = os.path.join(tmp, 'dedupe')
    os.makedirs(d, exist_ok=True)
    a = mod.unique_path(d, 's', '.mp3')
    open(a, 'wb').write(b'x')
    b = mod.unique_path(d, 's', '.mp3')
    check(os.path.basename(a) == 's.mp3', '首个路径')
    check(os.path.basename(b) == 's (1).mp3', '重名追加序号')

    print('\n== 4. DIDL-Lite 兜底解析（纯正则，不依赖 XML 库）==')
    didl = ('<DIDL-Lite xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
            '<item><dc:title>海阔天空</dc:title>'
            '<upnp:artist>Beyond</upnp:artist>'
            '<upnp:album>乐与怒</upnp:album></item></DIDL-Lite>')
    meta = mod.parse_didl(didl)
    check(meta.get('artist') == 'Beyond', 'DIDL 艺术家')
    check(meta.get('title') == '海阔天空', 'DIDL 标题')
    check(mod.parse_didl('not xml') == {}, '非法 XML 不抛异常')
    check(mod.parse_didl('') == {} and mod.parse_didl(None) == {}, '空输入')

    plain = ('<item><title>Plain Song</title><artist>A &amp; B</artist>'
             '<artist>ignored</artist></item>')
    check(mod.parse_didl(plain).get('artist') == 'A & B', '无命名空间前缀 + 实体反转义')
    pick = '<item><dc:creator>CreatorName</dc:creator><upnp:artist>RealArtist</upnp:artist></item>'
    check(mod.parse_didl(pick).get('artist') == 'RealArtist', 'upnp:artist 优先于 dc:creator')
    only_creator = '<item><dc:creator>OnlyCreator</dc:creator></item>'
    check(mod.parse_didl(only_creator).get('artist') == 'OnlyCreator', 'creator 兜底')
    attr = '<item><upnp:artist role="Performer">RoleArtist</upnp:artist></item>'
    check(mod.parse_didl(attr).get('artist') == 'RoleArtist', '带属性的标签')
    num = '<item><dc:title>Track &#65;&#x42;</dc:title></item>'
    check(mod.parse_didl(num).get('title') == 'Track AB', '数字实体')

    print('\n== 4b. 冻结宿主 import 白名单（回归守卫）==')
    # 探针实测：官方 Macast.exe (PyInstaller 4.8 / Python 3.7) 里可用的模块。
    # 若插件引入表外模块，宿主会静默丢弃整个插件。
    verified = {
        'os', 're', 'sys', 'uuid', 'time', 'gettext', 'logging', 'threading',
        'subprocess', 'urllib', 'enum', 'cherrypy',
        'json', 'base64', 'macast', 'macast_renderer',
    }
    tree = ast.parse(io.open(PLUGIN, encoding='utf-8').read())
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                roots.add(a.name.split('.')[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split('.')[0])
    bad = sorted(roots - verified)
    check(not bad, '插件只用宿主已验证可用的模块', '未验证={}'.format(bad))
    check('xml' not in roots, '未引用宿主缺失的 xml 包', '导入根={}'.format(sorted(roots)))

    print('\n== 5. 端到端：投送 → 缓存 → 重命名 → 通知 ==')
    cache = os.path.join(tmp, 'cache')
    _Setting.setting = {
        'Cache_Enable': 1, 'Cache_Dir': cache, 'Cache_AudioOnly': 1,
        'Cache_Toast': 1, 'Cache_NameFormat': '{artist}-{title}',
        'Cache_UserAgent': 'test-ua', 'Cache_MaxSizeMB': 512,
    }
    srv, base = start_server()
    Handler.payload = make_mp3_id3v2('周杰伦', '晴天', '叶惠美')
    r = mod.MusicCacheRenderer()
    check(r.last_url is None, '构造完成，尚未投送')

    url = base + '/stream/token123?sign=abc'
    r.set_media_url(url)
    check(r.last_url == url, '父类播放未被阻塞')

    name = wait_file(cache)
    check(name == '周杰伦-晴天.mp3', '落盘文件名', '实际={!r}'.format(name))
    check(os.path.getsize(os.path.join(cache, name)) > 3000, '文件内容完整')
    check(any('缓存完毕' in str(n) for n in NOTIFICATIONS), '已发出缓存完毕通知')
    check(any('缓存完毕' in t for t in r.toasts), '播放器内提示已显示')
    check(os.path.isdir(os.path.join(cache, '.macast_cache_tmp')), '临时目录已建')
    check(not [x for x in os.listdir(os.path.join(cache, '.macast_cache_tmp'))
               if x.endswith('.part')], '临时 .part 已清理')

    print('\n== 6. 同一 URL 不重复缓存 ==')
    before = sorted(os.listdir(cache))
    r.set_media_url(url)
    time.sleep(2.5)
    check(sorted(os.listdir(cache)) == before, '重复投送被去重')

    print('\n== 7. 非音频按配置跳过 ==')
    before7 = sorted(os.listdir(cache))
    r.set_media_url(base + '/video.mp4?x=1')
    time.sleep(4)
    after7 = sorted(os.listdir(cache))
    check(after7 == before7, 'video/mp4 未被缓存', '新增={}'.format(
        [n for n in after7 if n not in before7]))

    print('\n== 8. DIDL 兜底命名（文件无内嵌标签）==')
    Handler.payload = b'\xff\xfb\x90\x00' + b'\x00' * 70000
    _StubProtocol.metadata = didl
    r2 = mod.MusicCacheRenderer()
    r2.set_media_url(base + '/stream2/abcdef')
    name2 = wait_file(cache)
    # 上一次的 mp3 仍在，可能先被 wait_file 取到，改为轮询新文件
    end = time.time() + 25
    while time.time() < end:
        cand = [n for n in os.listdir(cache) if 'Beyond' in n]
        if cand:
            break
        time.sleep(0.2)
    check(bool(cand), 'DIDL 兜底生成 Beyond 命名', '文件列表={}'.format(os.listdir(cache)))
    _StubProtocol.metadata = ''

    print('\n== 9. 命名模板切换 ==')
    _Setting.setting['Cache_NameFormat'] = '{title}-{artist}'
    Handler.payload = make_mp3_id3v2('Coldplay', 'Yellow', 'Parachutes')
    r3 = mod.MusicCacheRenderer()
    r3.set_media_url(base + '/stream3/zzz')
    end = time.time() + 25
    hit = ''
    while time.time() < end:
        cand = [n for n in os.listdir(cache) if n.startswith('Yellow')]
        if cand:
            hit = cand[0]
            break
        time.sleep(0.2)
    check(hit == 'Yellow-Coldplay.mp3', '歌名-艺术家 模板', '实际={!r}'.format(hit))

    print('\n== 10. Macast 插件发现机制（真实 importlib 路径）==')
    fake_cfg = os.path.join(tmp, 'cfg')
    rdir = os.path.join(fake_cfg, 'renderer')
    os.makedirs(rdir, exist_ok=True)
    open(os.path.join(rdir, '__init__.py'), 'a').close()
    shutil.copyfile(PLUGIN, os.path.join(rdir, 'music_cache.py'))
    sys.path.append(fake_cfg)
    m2 = importlib.import_module('renderer.music_cache')
    check(getattr(m2, 'MusicCacheRenderer', None) is not None,
          'importlib 以 renderer.<文件名> 载入')

    src = io.open(os.path.join(rdir, 'music_cache.py'), encoding='utf-8').read()
    meta = dict(re.findall("<macast.(.*?)>(.*?)</macast", src))
    check(meta.get('title') == '音乐缓存', 'macast.title 元数据（渲染器显示名）')
    check(meta.get('renderer') == 'MusicCacheRenderer', 'macast.renderer 元数据（渲染器类名）')
    check('win32' in meta.get('platform', ''), '平台元数据覆盖 win32')
    check(bool(meta.get('desc')) and bool(meta.get('version')), '简介/版本号非空')
    plugin_cls = getattr(m2, meta['renderer'], None)
    check(plugin_cls is not None, '按元数据取到渲染器类')
    inst2 = plugin_cls()  # Macast 的 get_instance() 就是无参实例化
    check(type(inst2).__name__ == 'MusicCacheRenderer', '无参实例化成功')
    check(callable(getattr(inst2, 'set_media_url', None)), 'set_media_url 可被协议调用')
    menu = inst2.renderer_setting.build_menu()
    labels = [getattr(i, 'text', None) for i in menu]
    check('音乐缓存' in labels, '设置菜单已挂载「音乐缓存」分组', str(labels))

    print('\n== 11. 安装器：路径与设置文件编码（回归守卫）==')
    root = os.path.dirname(os.path.dirname(PLUGIN))
    ispec = importlib.util.spec_from_file_location(
        'mcp_install', os.path.join(root, 'install.py'))
    inst = importlib.util.module_from_spec(ispec)
    ispec.loader.exec_module(inst)

    saved = dict(os.environ)
    try:
        fake_local = os.path.join(tmp, 'localappdata')
        fake_roam = os.path.join(tmp, 'roaming')
        os.makedirs(fake_local, exist_ok=True)
        os.makedirs(fake_roam, exist_ok=True)
        os.environ['LOCALAPPDATA'] = fake_local
        os.environ['APPDATA'] = fake_roam
        got = inst.setting_dir()
        check(got.lower().startswith(fake_local.lower()),
              'setting_dir 用 LOCALAPPDATA（appdirs roaming=False）', '实际={}'.format(got))

        cfg = os.path.join(fake_local, 'xfangfang', 'Macast')
        os.makedirs(cfg, exist_ok=True)
        path = os.path.join(cfg, 'macast_setting.json')
        # 模拟"Macast 写的纯 ASCII 配置"
        with io.open(path, 'w', encoding='ascii') as f:
            json.dump({'DLNA_FriendlyName': 'Macast(Kiri\u7684PC)',
                       'Macast_Renderer': 'MPV Renderer',
                       'ApplicationPort': 61660}, f, ensure_ascii=True)
        inst.patch_setting(cfg, preset_dir=os.path.join(tmp, 'music'), select=True)

        raw = io.open(path, 'rb').read()
        check(all(b < 128 for b in raw), '残留配置文件保持纯 ASCII')
        data = json.loads(raw.decode('ascii'))
        check(data['DLNA_FriendlyName'] == 'Macast(Kiri\u7684PC)',
              '中文 DLNA_FriendlyName 未被破坏',
              '实际={!r}'.format(data['DLNA_FriendlyName']))
        check(data['Macast_Renderer'] == '音乐缓存', '已切换渲染器')
        check(data['Cache_Dir'].endswith('music'), '已写入缓存目录')
        check(os.path.exists(path + '.bak'), '已生成 .bak 备份')

        # 即使原文件是裸 UTF-8，也必须被规范成纯 ASCII
        with io.open(path, 'w', encoding='utf-8') as f:
            f.write('{"DLNA_FriendlyName": "\\u4e2d\\u6587", "Macast_Renderer": "MPV"}')
        inst.patch_setting(cfg, select=True)
        raw2 = io.open(path, 'rb').read()
        check(all(b < 128 for b in raw2), '裸 UTF-8 输入被规范为纯 ASCII')
        check(json.loads(raw2.decode('ascii'))['DLNA_FriendlyName'] == '\u4e2d\u6587',
              '规范化后中文仍正确')
    finally:
        os.environ.clear()
        os.environ.update(saved)

    print('\n== 12. 生命周期与残留清理（回归守卫）==')
    life_cache = os.path.join(tmp, 'life_cache')
    _Setting.setting['Cache_Dir'] = life_cache

    # 制造上次被强杀残留的 .part
    tmpd = os.path.join(life_cache, '.macast_cache_tmp')
    os.makedirs(tmpd, exist_ok=True)
    with io.open(os.path.join(tmpd, 'deadbeef.part'), 'wb') as f:
        f.write(b'x' * 128)

    r4 = mod.MusicCacheRenderer()
    r4.start()
    check(r4._stopping is False, 'start() 后停止标志已复位')
    left = [n for n in os.listdir(tmpd) if n.endswith('.part')]
    check(not left, '首次 start() 清理上次残留的 .part', '剩余={}'.format(left))

    # 模拟 reload：stop() -> start()，缓存必须仍然可用
    _Setting.setting['Cache_NameFormat'] = '{artist}-{title}'
    Handler.payload = make_mp3_id3v2('测试', '重载后仍缓存', '')
    r4.stop()
    check(r4._stopping is True, 'stop() 置位停止标志')
    r4.start()
    check(r4._stopping is False, 'start() 复位停止标志（否则 reload 后缓存永久失效）')
    r4.set_media_url(base + '/life/x')
    hit2 = ''
    end = time.time() + 25
    while time.time() < end:
        cand = [n for n in os.listdir(life_cache) if not n.startswith('.')]
        if cand:
            hit2 = cand[0]
            break
        time.sleep(0.2)
    check(hit2 == '测试-重载后仍缓存.mp3', 'reload 之后缓存链路仍然可用',
          '实际={!r}'.format(hit2))

    # reload 不能清掉正在写入的 .part
    with io.open(os.path.join(tmpd, 'inflight.part'), 'wb') as f:
        f.write(b'y')
    r4.stop()
    r4.start()
    check(os.path.exists(os.path.join(tmpd, 'inflight.part')),
          'reload 不清除在途的 .part')

    srv.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)

    print('\n================ 结果 ================')
    print('检查项：{}　失败：{}'.format(CHECKS, len(FAILURES)))
    if FAILURES:
        for f in FAILURES:
            print('  失败项：' + f)
        return 1
    print('全部通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())
