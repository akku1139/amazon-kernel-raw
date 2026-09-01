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
ASSET_DIR = Path(tempfile.gettempdir()) / "kindle_src_assets"

def slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r'[^a-z0-9]+', '-', text).strip('-')
    return text

def make_tag(device_name: str) -> str:
    slug = slugify(device_name)
    max_slug_len = TAG_MAX_LEN - len(PREFIX)
    if len(slug) > max_slug_len:
        hash_ = hashlib.md5(device_name.encode()).hexdigest()[:8]
        slug = slug[:max_slug_len - 9] + '-' + hash_
    return PREFIX + slug

def version_key(entry: dict) -> list:
    v = entry.get('version', '')
    parts = [int(p) if p.isdigit() else 0 for p in v.split('.')]
    while len(parts) < 10:
        parts.append(0)
    return parts

def parse_version(filename: str) -> tuple:
    m = re.search(r'Kindle_src_([\d.]+)_(\d+)\.tar\.gz$', filename)
    if m:
        return m.group(1), m.group(2)
    return '', ''

def fetch_and_parse() -> dict:
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
    sorted_entries = sorted(entries, key=version_key, reverse=True)
    for e in sorted_entries:
        version = e.get('version', '') or 'N/A'
        build = e.get('build', '') or 'N/A'
        # リリースアセットへの相対リンク（GitHubが自動的にアセットへのリンクを生成）
        lines.append(f"| {version} | {build} | `{e['filename']}` |")
    return "\n".join(lines)

def asset_exists(tag: str, filename: str) -> bool:
    """指定タグのリリースにアセットが既にあるか確認"""
    result = subprocess.run(
        ['gh', 'release', 'view', tag, '--json', 'assets', '--jq', '.assets[].name'],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return False
    assets = result.stdout.splitlines()
    return filename in assets

def download_file(url: str, dest: Path):
    """URLからファイルをダウンロードしてdestに保存"""
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(dest, 'wb') as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)

def process_device(tag: str, device_name: str, current_entries: list, state: dict) -> bool:
    """1デバイス分を処理し、状態を更新する。成功ならTrueを返す"""
    existing = state.get(tag, [])
    existing_filenames = {e['filename'] for e in existing}

    # 新規エントリのみを処理
    new_entries = [e for e in current_entries if e['filename'] not in existing_filenames]
    if not new_entries:
        print(f"No new entries for {tag}, skipping.")
        return False

    # リリースの存在確認と作成/更新
    view_result = subprocess.run(['gh', 'release', 'view', tag], capture_output=True, text=True)
    release_exists = view_result.returncode == 0

    # まずリリースノートを更新（新規作成なら作成）
    merged = existing + new_entries
    seen = set()
    merged_unique = []
    for e in merged:
        if e['filename'] not in seen:
            seen.add(e['filename'])
            merged_unique.append(e)
    merged_sorted = sorted(merged_unique, key=version_key, reverse=True)

    body = release_body(device_name, merged_sorted)
    with tempfile.NamedTemporaryFile('w', delete=False, suffix='.md') as f:
        f.write(body)
        notes_file = f.name

    try:
        if release_exists:
            subprocess.run(['gh', 'release', 'edit', tag, '--notes-file', notes_file], check=True)
            print(f"Updated release notes: {tag}")
        else:
            subprocess.run(['gh', 'release', 'create', tag,
                            '--title', device_name,
                            '--notes-file', notes_file], check=True)
            print(f"Created release: {tag}")
    finally:
        os.unlink(notes_file)

    # 各新規ファイルをダウンロードしてアップロード
    ASSET_DIR.mkdir(exist_ok=True)
    for entry in new_entries:
        filename = entry['filename']
        if asset_exists(tag, filename):
            print(f"Asset {filename} already exists, skipping upload.")
        else:
            # ダウンロード
            local_path = ASSET_DIR / filename
            print(f"Downloading {filename}...")
            download_file(entry['url'], local_path)
            # アップロード
            print(f"Uploading {filename} to {tag}...")
            subprocess.run(['gh', 'release', 'upload', tag, str(local_path)],
                           check=True)
            # 一時ファイル削除
            local_path.unlink(missing_ok=True)
        # 成功したエントリを状態に追加
        existing.append(entry)

    # 状態を更新
    state[tag] = sorted(existing, key=version_key, reverse=True)
    return True

def main():
    # 状態読み込み
    state = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            state = json.load(f)

    devices = fetch_and_parse()
    any_change = False

    for tag, (device_name, entries) in devices.items():
        print(f"\n=== Processing {device_name} (tag: {tag}) ===")
        changed = process_device(tag, device_name, entries, state)
        if changed:
            any_change = True
            # デバイスごとに状態を保存＆コミット
            with open(STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=2)
            subprocess.run(['git', 'add', STATE_FILE], check=True)
            diff = subprocess.run(['git', 'diff', '--cached', '--quiet'],
                                  capture_output=True)
            if diff.returncode != 0:
                subprocess.run(['git', 'commit', '-m',
                                f"Update state after processing {tag}"],
                               check=True)
                subprocess.run(['git', 'push'], check=True)
                print(f"State committed for {tag}")
            else:
                print(f"No state change to commit for {tag}")

    if not any_change:
        print("No new sources found anywhere.")

if __name__ == '__main__':
    main()