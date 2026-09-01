#!/usr/bin/env python3
import json
import os
import re
import subprocess
import tempfile
import hashlib
from pathlib import Path

import requests
from bs4 import BeautifulSoup

SOURCE_URL = "https://digprjsurvey.amazon.com/csad/help/node/200203720"
STATE_FILE = "releases_state.json"
PREFIX = "ks-"
TAG_MAX_LEN = 40


def slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r'[^a-z0-9]+', '-', text).strip('-')
    return text


def make_tag(device_name: str) -> str:
    """デバイス名から安定したリリースタグを生成する"""
    slug = slugify(device_name)
    max_slug_len = TAG_MAX_LEN - len(PREFIX)
    if len(slug) > max_slug_len:
        hash_ = hashlib.md5(device_name.encode()).hexdigest()[:8]
        slug = slug[:max_slug_len - 9] + '-' + hash_
    return PREFIX + slug


def version_key(entry: dict) -> list:
    """バージョン文字列を数値タプルに変換してソート用キーにする"""
    v = entry.get('version', '')
    parts = [int(p) if p.isdigit() else 0 for p in v.split('.')]
    # 最大10要素にパディング
    while len(parts) < 10:
        parts.append(0)
    return parts


def parse_version(filename: str) -> tuple:
    """ファイル名からバージョンとビルド番号を抽出する"""
    m = re.search(r'Kindle_src_([\d.]+)_(\d+)\.tar\.gz$', filename)
    if m:
        return m.group(1), m.group(2)
    return '', ''


def fetch_and_parse() -> dict:
    """ページを取得し、デバイスごとのエントリリストを返す"""
    resp = requests.get(SOURCE_URL, headers={'User-Agent': 'Mozilla/5.0'}, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, 'html.parser')

    devices = {}
    for h2 in soup.find_all('h2', class_='sectiontitle'):
        device_name = h2.get_text(strip=True)
        ul = h2.find_next('ul')
        if not ul:
            continue

        entries = []
        for a in ul.find_all('a', href=True):
            url = a['href'].strip()
            filename = url.split('/')[-1]
            version, build = parse_version(filename)
            entries.append({
                'filename': filename,
                'url': url,
                'version': version,
                'build': build
            })

        if entries:
            tag = make_tag(device_name)
            devices[tag] = (device_name, entries)

    return devices


def release_body(device_name: str, entries: list) -> str:
    """Release本文をMarkdown形式で生成する"""
    lines = [
        f"# Kindle Source Code Releases - {device_name}",
        "",
        "Automatically updated from [Amazon Source Code Notice](https://digprjsurvey.amazon.com/csad/help/node/200203720).",
        "",
        "## Sources",
        "",
        "| Version | Build | Download |",
        "|---------|-------|----------|"
    ]

    # バージョン降順にソート
    sorted_entries = sorted(entries, key=version_key, reverse=True)
    for e in sorted_entries:
        version = e.get('version', '') or 'N/A'
        build = e.get('build', '') or 'N/A'
        url = e.get('url', '')
        filename = e.get('filename', '')
        lines.append(f"| {version} | {build} | [{filename}]({url}) |")

    return "\n".join(lines)


def main():
    # stateファイル読み込み
    state = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            state = json.load(f)

    devices = fetch_and_parse()
    changed = False

    for tag, (device_name, current_entries) in devices.items():
        existing = state.get(tag, [])
        existing_filenames = {e['filename'] for e in existing}
        new_entries = [e for e in current_entries if e['filename'] not in existing_filenames]

        if not new_entries:
            continue

        # 既存と新規をマージ
        merged = existing + new_entries
        # ファイル名で重複除去（念のため）
        seen = set()
        merged_unique = []
        for e in merged:
            if e['filename'] not in seen:
                seen.add(e['filename'])
                merged_unique.append(e)
        merged = sorted(merged_unique, key=version_key, reverse=True)

        state[tag] = merged
        body = release_body(device_name, merged)

        # 一時ファイルに本文を書き出し
        with tempfile.NamedTemporaryFile('w', delete=False, suffix='.md') as f:
            f.write(body)
            notes_file = f.name

        try:
            # release存在確認
            view = subprocess.run(
                ['gh', 'release', 'view', tag],
                capture_output=True, text=True
            )
            if view.returncode == 0:
                # 更新
                subprocess.run(
                    ['gh', 'release', 'edit', tag, '--notes-file', notes_file],
                    check=True
                )
                print(f"Updated release: {tag}")
            else:
                # 新規作成
                subprocess.run(
                    ['gh', 'release', 'create', tag,
                     '--title', device_name,
                     '--notes-file', notes_file],
                    check=True
                )
                print(f"Created release: {tag}")
        finally:
            os.unlink(notes_file)

        changed = True

    if changed:
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2)
        print("State updated.")
    else:
        print("No new sources found.")


if __name__ == '__main__':
    main()