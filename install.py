#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Macast 音乐缓存插件安装器
# Copyright (C) 2026 KiriChen-Wind
# SPDX-License-Identifier: GPL-3.0-or-later
"""Macast 音乐缓存插件安装器（跨平台，仅标准库）。

用法：
    python install.py                    # 安装插件
    python install.py --dir "D:\\Music"   # 安装并预设缓存目录
    python install.py --select           # 安装并把 Macast 渲染器切换为本插件
    python install.py --uninstall        # 卸载
    python install.py --where            # 只打印路径，不动作
"""

import os
import sys
import json
import shutil
import argparse

PLUGIN_NAME = 'music_cache.py'
PLUGIN_TITLE = '音乐缓存'


def setting_dir():
    """与 Macast 的 appdirs.user_config_dir('Macast', 'xfangfang') 完全一致。

    注意：appdirs 的 user_config_dir 默认 roaming=False，Windows 上走
    CSIDL_LOCAL_APPDATA（即 %LOCALAPPDATA%），**不是** %APPDATA% 下的 Roaming。
    装到 Roaming 的话 Macast 永远扫不到插件。

    另外 Macast 的 Setting.load() 是 open(path) 不带 encoding，Windows 上按
    系统 ANSI 代码页解码，因此 macast_setting.json 必须保持纯 ASCII
    （Macast 自己用 json.dump(ensure_ascii=True) 写）。
    """
    if sys.platform == 'win32':
        base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~\\AppData\\Local')
        return os.path.join(base, 'xfangfang', 'Macast')
    if sys.platform == 'darwin':
        return os.path.join(os.path.expanduser('~'), 'Library', 'Application Support', 'Macast')
    base = os.environ.get('XDG_CONFIG_HOME') or os.path.join(os.path.expanduser('~'), '.config')
    return os.path.join(base, 'Macast')


def plugin_source():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'renderer', PLUGIN_NAME)


def install(args):
    cfg = setting_dir()
    renderer_dir = os.path.join(cfg, 'renderer')
    os.makedirs(renderer_dir, exist_ok=True)
    init_file = os.path.join(renderer_dir, '__init__.py')
    if not os.path.exists(init_file):
        open(init_file, 'a').close()

    src, dst = plugin_source(), os.path.join(renderer_dir, PLUGIN_NAME)
    if not os.path.exists(src):
        print('错误：找不到插件源文件：{}'.format(src))
        return 1
    shutil.copyfile(src, dst)
    print('成功：插件已安装：{}'.format(dst))
    warn_stale_roaming()

    patch_setting(cfg, preset_dir=args.dir, select=args.select)
    print('\n下一步：重启 Macast，在托盘菜单「设置 → 选择播放器」里选「{}」。'.format(PLUGIN_TITLE))
    print('缓存开关和目录在托盘菜单的「音乐缓存」分组里。')
    return 0


def warn_stale_roaming():
    """提醒曾经装错到 Roaming 的情况（appdirs 用的是 LOCALAPPDATA）。"""
    if sys.platform != 'win32':
        return
    roaming = os.environ.get('APPDATA')
    if not roaming:
        return
    stale = os.path.join(roaming, 'xfangfang', 'Macast', 'renderer', PLUGIN_NAME)
    if os.path.exists(stale):
        print('警告：发现旧安装残留（Macast 不会读取此路径）：{}'.format(stale))
        print('    可安全删除：Remove-Item -Recurse -Force "{}"'.format(
            os.path.dirname(os.path.dirname(stale))))


def patch_setting(cfg, preset_dir=None, select=False):
    path = os.path.join(cfg, 'macast_setting.json')
    data = {}
    if os.path.exists(path):
        try:
            # Macast 的 Setting.load() 是 open(path) 不带 encoding，
            # Windows 上按系统 ANSI 代码页解码，所以这个文件必须保持纯 ASCII。
            # 用 utf-8 读（容错），再用 ensure_ascii=True 写回（与 Macast 一致）。
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                data = json.load(f)
            shutil.copyfile(path, path + '.bak')
        except Exception as e:
            print('警告：无法读取 macast_setting.json（{}），跳过写入'.format(e))
            return
    changed = False
    if select and data.get('Macast_Renderer') != PLUGIN_TITLE:
        data['Macast_Renderer'] = PLUGIN_TITLE
        changed = True
    if preset_dir:
        data['Cache_Dir'] = os.path.abspath(preset_dir)
        changed = True
    if not changed:
        return
    try:
        os.makedirs(cfg, exist_ok=True)
        with open(path, 'w', encoding='ascii') as f:
            json.dump(data, f, sort_keys=True, indent=4, ensure_ascii=True)
        print('成功：已更新设置：{}'.format(path))
    except Exception as e:
        print('警告：写入设置失败：{}'.format(e))


def uninstall():
    cfg = setting_dir()
    dst = os.path.join(cfg, 'renderer', PLUGIN_NAME)
    if os.path.exists(dst):
        os.remove(dst)
        print('移除：{}'.format(dst))
    else:
        print('提示：未安装：{}'.format(dst))
    default_dir = os.path.join(os.path.expanduser('~'), 'Music', 'MacastCache')
    print('提示：缓存文件保留在 {}，如需清理请手动删除。'.format(default_dir))
    return 0


def main():
    ap = argparse.ArgumentParser(description='安装 Macast 音乐缓存插件')
    ap.add_argument('--dir', help='预设缓存目录')
    ap.add_argument('--select', action='store_true', help='同时把 Macast 渲染器切换为本插件')
    ap.add_argument('--uninstall', action='store_true', help='卸载插件')
    ap.add_argument('--where', action='store_true', help='打印 Macast 配置目录')
    args = ap.parse_args()

    print('Macast 配置目录：{}'.format(setting_dir()))
    if args.where:
        return 0
    if args.uninstall:
        return uninstall()
    return install(args)


if __name__ == '__main__':
    sys.exit(main())
