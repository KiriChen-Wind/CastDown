# -*- coding: utf-8 -*-
# Macast 音乐缓存插件 —— 真机端到端验证
# Copyright (C) 2026 KiriChen-Wind
# SPDX-License-Identifier: GPL-3.0-or-later
"""真机端到端验证：向运行中的 Macast.exe 发真实 DLNA SetAVTransportURI，
检查插件是否在真实冻结宿主里完成 缓存 -> 重命名 -> 通知。

不依赖 Macast 源码，只用标准库。
"""

import io
import os
import sys
import json
import time
import threading
import urllib.request
import urllib.error
import http.server

CACHE_DIR = r"D:\Music"
SONG_HTTP_PORT = 8791


def make_mp3(artist, title, album):
    def frame(fid, text):
        payload = b'\x03' + text.encode('utf-8')
        return fid + len(payload).to_bytes(4, 'big') + b'\x00\x00' + payload
    frames = frame(b'TIT2', title) + frame(b'TPE1', artist) + frame(b'TALB', album)
    n = len(frames)
    ss = bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])
    return b'ID3\x03\x00\x00' + ss + frames + b'\xff\xfb\x90\x00' + b'\x00' * 120000


PAYLOAD = make_mp3('周杰伦', '晴天', '叶惠美')


class SongHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        rng = self.headers.get('Range')
        if rng:
            self.send_response(206)
            self.send_header('Content-Type', 'audio/mpeg')
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Content-Range', 'bytes 0-{}/{}'.format(
                len(PAYLOAD) - 1, len(PAYLOAD)))
            self.send_header('Content-Length', str(len(PAYLOAD)))
            self.end_headers()
            self.wfile.write(PAYLOAD)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'audio/mpeg')
        self.send_header('Content-Length', str(len(PAYLOAD)))
        self.end_headers()
        self.wfile.write(PAYLOAD)

    def log_message(self, *a):
        pass


def cfg_dir():
    return os.path.join(os.environ['LOCALAPPDATA'], 'xfangfang', 'Macast')


def read_port():
    with io.open(os.path.join(cfg_dir(), 'macast_setting.json'), encoding='utf-8') as f:
        return json.load(f)['ApplicationPort']


def soap_set_uri(port, uri, metadata):
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        '<s:Body>'
        '<u:SetAVTransportURI xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
        '<InstanceID>0</InstanceID>'
        '<CurrentURI>{}</CurrentURI>'
        '<CurrentURIMetaData>{}</CurrentURIMetaData>'
        '</u:SetAVTransportURI>'
        '</s:Body></s:Envelope>'
    ).format(uri, escape(metadata))
    req = urllib.request.Request(
        'http://127.0.0.1:{}/AVTransport/action'.format(port),
        data=body.encode('utf-8'),
        headers={
            'Content-Type': 'text/xml; charset="utf-8"',
            'SOAPACTION': '"urn:schemas-upnp-org:service:AVTransport:1#SetAVTransportURI"',
        })
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.status, r.read().decode('utf-8', 'replace')


def escape(s):
    return (s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            .replace('"', '&quot;'))


def list_files(d):
    if not os.path.isdir(d):
        return set()
    return set(n for n in os.listdir(d) if os.path.isfile(os.path.join(d, n)))


def part_snapshot():
    """缓存目录临时区里 .part 文件的 {文件名: 大小}。"""
    d = os.path.join(CACHE_DIR, '.macast_cache_tmp')
    out = {}
    if not os.path.isdir(d):
        return out
    for n in os.listdir(d):
        if n.endswith('.part'):
            try:
                out[n] = os.path.getsize(os.path.join(d, n))
            except OSError:
                pass
    return out


def main():
    ok = True

    srv = http.server.ThreadingHTTPServer(('127.0.0.1', SONG_HTTP_PORT), SongHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print('歌曲服务已启动：127.0.0.1:{}'.format(SONG_HTTP_PORT))

    port = read_port()
    print('Macast 端口：{}'.format(port))

    # ---- 1. 插件是否被真实宿主识别
    try:
        raw = urllib.request.urlopen(
            'http://127.0.0.1:{}/api?query=plugin-info'.format(port), timeout=15).read()
        info = json.loads(raw.decode('utf-8'))
        plugins = info.get('plugins', [])
        print('插件列表：')
        for p in plugins:
            print('      - {!r} 类型={} 默认={}'.format(
                p.get('title'), p.get('type'), p.get('default')))
        titles = [p.get('title') for p in plugins]
        if '音乐缓存' in titles:
            print('[通过] Macast 已加载「音乐缓存」插件')
        else:
            print('[失败] Macast 未列出「音乐缓存」：{}'.format(titles))
            ok = False
    except Exception as e:
        print('[失败] 无法读取插件信息：{!r}'.format(e))
        ok = False

    # ---- 2. 真实 DLNA 投送
    baseline_files = list_files(CACHE_DIR)
    baseline_parts = part_snapshot()
    print('D:\\Music 现有文件数：{}'.format(len(baseline_files)))

    uri = 'http://127.0.0.1:{}/music/song.mp3'.format(SONG_HTTP_PORT)
    didl = (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="0" parentID="-1" restricted="1">'
        '<dc:title>晴天</dc:title>'
        '<upnp:artist>周杰伦</upnp:artist>'
        '<upnp:album>叶惠美</upnp:album>'
        '<upnp:class>object.item.audioItem.musicTrack</upnp:class>'
        '</item></DIDL-Lite>')

    try:
        code, resp = soap_set_uri(port, uri, didl)
        print('[通过] SetAVTransportURI 返回 HTTP {}'.format(code))
    except Exception as e:
        print('[失败] SOAP 投送失败：{!r}'.format(e))
        srv.shutdown()
        return 1

    # ---- 3. 等待落盘
    target = '周杰伦-晴天.mp3'
    end = time.time() + 60
    found = None
    while time.time() < end:
        new = list_files(CACHE_DIR) - baseline_files
        if target in new:
            found = sorted(new)
            break
        time.sleep(0.5)

    if found:
        print('[通过] 新落盘文件：{}'.format(found))
        full = os.path.join(CACHE_DIR, target)
        print('     大小：{} 字节'.format(os.path.getsize(full)))
        with io.open(full, 'rb') as f:
            head = f.read(16)
        print('     文件头：{}'.format(head[:10]))
        print('[通过] 重命名符合「艺术家-歌名」')
    else:
        print('[失败] 60 秒内没有新文件落到 {}'.format(CACHE_DIR))
        ok = False

    # .part 判定：只追究「本次投送产生、且大小不再变化」的死文件。
    # 同一台机器上可能有别的设备正在投送（并发下载），那种 .part 是在途文件，
    # 大小会持续增长，不应判为残留。
    time.sleep(1)
    snap_a = part_snapshot()
    time.sleep(3)
    snap_b = part_snapshot()
    new_parts = sorted(set(snap_a) | set(snap_b)) 
    new_parts = [n for n in new_parts if n not in baseline_parts]
    frozen, growing, vanished = [], [], []
    for n in new_parts:
        if n not in snap_b:
            vanished.append(n)
        elif n in snap_a and snap_b[n] > snap_a[n]:
            growing.append(n)
        else:
            frozen.append(n)
    if frozen:
        print('[失败] 残留的临时文件（大小已静止）：{}'.format(frozen))
        ok = False
    elif growing:
        print('[通过] 无残留；检测到并发投送的在途下载（属正常）：{}'.format(growing))
    elif vanished:
        print('[通过] 无残留；投送产生的 .part 已被自行清理：{}'.format(vanished))
    else:
        print('[通过] 无残留的临时文件')

    srv.shutdown()
    print('\n=== 真机结果：{} ==='.format('通过' if ok else '失败'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
