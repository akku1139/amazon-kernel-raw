#!/usr/bin/env python3
import json
import logging
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

# ログ設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)


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
    """
    ファイル名からバージョンとビルド番号を抽出する。
    例:
      Kindle_src_5.18.5_4546050025.tar.gz -> ("5.18.5", "4546050025")
      Fire_HD10_13th_Gen-8.3.3.0-20241028.tar.bz2 -> ("8.3.3.0", "20241028")
    """
    # 拡張子を除去
    path = Path(filename)
    suffixes = path.suffixes
    base = path.name
    # .tar.gz, .tar.bz2 などに対応
    if suffixes:
        # 最後の拡張子(.gz, .bz2, .xz)を除去
        last_ext = suffixes[-1]
        base = base[:-len(last_ext)]
        # さらに.tarを除去
        if base.endswith('.tar'):
            base = base[:-4]

    # バージョン（数字.数字...）の後に - か _ で区切られた数字（ビルド）を探す
    m = re.search(r'(\d+(?:\.\d+)+)[-_](\d+)', base)
    if m:
        version = m.group(1)
        build = m.group(2)
        return version, build

    # バージョンのみ
    m = re.search(r'(\d+(?:\.\d+)+)', base)
    if m:
        return m.group(1), ''

    return '', ''


def fetch_and_parse() -> dict:
    """ページを取得し、デバイスごとのエントリリストを返す"""
    logger.info(f"Fetching {SOURCE_URL}")
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
                'build': build,
                'uploaded': False,   # 初期値。後で更新
                'error': None
            })

        if entries:
            tag = make_tag(device_name)
            devices[tag] = (device_name, entries)

    logger.info(f"Found {len(devices)} devices")
    return devices


def release_body(device_name: str, entries: list) -> str:
    """リリースノート本文を生成する。uploadedフラグに応じてリンクを変える"""
    lines = [
        f"# Kindle Source Code Releases - {device_name}",
        "",
        "Automatically updated from [Amazon Source Code Notice](https://digprjsurvey.amazon.com/csad/help/node/200203720).",
        "",
        "## Sources",
        "",
        "| Version | Build | Asset | Source URL |",
        "|---------|-------|-------|------------|"
    ]

    sorted_entries = sorted(entries, key=version_key, reverse=True)
    for e in sorted_entries:
        version = e.get('version') or 'N/A'
        build = e.get('build') or 'N/A'
        filename = e['filename']
        url = e['url']

        if e.get('uploaded', False):
            asset_link = f"[{filename}](../../releases/download/{make_tag(device_name)}/{filename})"
        else:
            asset_link = "Not uploaded"
            if e.get('error'):
                asset_link += f" ({e['error']})"

        source_link = f"[{filename}]({url})"
        lines.append(f"| {version} | {build} | {asset_link} | {source_link} |")

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
    """ファイルをダウンロードする。失敗時は例外を投げる"""
    logger.info(f"Downloading {url}")
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dest, 'wb') as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
    logger.info(f"Downloaded to {dest}")


def upload_file(tag: str, local_path: Path):
    """gh release uploadを実行。失敗時はCalledProcessError"""
    logger.info(f"Uploading {local_path.name} to {tag}")
    subprocess.run(['gh', 'release', 'upload', tag, str(local_path)], check=True)
    logger.info(f"Uploaded {local_path.name}")


def save_state_and_push(state: dict):
    """状態をファイルに保存し、変更があればコミット＆プッシュ"""
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2)
    subprocess.run(['git', 'add', STATE_FILE], check=True)
    diff = subprocess.run(['git', 'diff', '--cached', '--quiet'], capture_output=True)
    if diff.returncode != 0:
        subprocess.run(['git', 'commit', '-m', 'Update release state'], check=True)
        subprocess.run(['git', 'push'], check=True)
        logger.info("State committed and pushed")
    else:
        logger.debug("No state changes to commit")


def process_device(tag: str, device_name: str, current_entries: list, state: dict):
    """1デバイス分を処理する。各ファイルごとに状態を保存する"""
    existing = state.get(tag, [])
    existing_filenames = {e['filename'] for e in existing}

    new_entries = [e for e in current_entries if e['filename'] not in existing_filenames]
    if not new_entries:
        logger.info(f"No new entries for {tag}, skipping.")
        return

    logger.info(f"Processing {device_name} (tag: {tag}), {len(new_entries)} new file(s)")

    # リリースが存在するか確認
    view_result = subprocess.run(['gh', 'release', 'view', tag], capture_output=True, text=True)
    release_exists = view_result.returncode == 0

    if not release_exists:
        # まず空のリリースを作成（アップロード先を確保）
        logger.info(f"Creating release {tag}")
        with tempfile.NamedTemporaryFile('w', delete=False, suffix='.md') as f:
            f.write(f"# {device_name}\n\nInitializing...")
            notes_file = f.name
        subprocess.run(['gh', 'release', 'create', tag,
                        '--title', device_name,
                        '--notes-file', notes_file], check=True)
        os.unlink(notes_file)

    # 各新規ファイルを処理
    ASSET_DIR.mkdir(exist_ok=True)
    for entry in new_entries:
        filename = entry['filename']
        logger.info(f"--- Processing file: {filename} ---")

        # 既にアセットが存在するか確認
        if asset_exists(tag, filename):
            logger.info(f"Asset {filename} already exists, skipping upload.")
            entry['uploaded'] = True
        else:
            # ダウンロード
            local_path = ASSET_DIR / filename
            try:
                download_file(entry['url'], local_path)
            except Exception as e:
                logger.error(f"Download failed for {filename}: {e}")
                entry['uploaded'] = False
                entry['error'] = f"Download error: {e}"
            else:
                # アップロード
                try:
                    upload_file(tag, local_path)
                    entry['uploaded'] = True
                except subprocess.CalledProcessError as e:
                    logger.error(f"Upload failed for {filename}: {e}")
                    entry['uploaded'] = False
                    entry['error'] = f"Upload error: {e.stderr.strip() if e.stderr else 'unknown'}"
                finally:
                    # 一時ファイル削除
                    if local_path.exists():
                        local_path.unlink()

        # 状態に追加
        existing.append(entry)
        state[tag] = sorted(existing, key=version_key, reverse=True)
        save_state_and_push(state)
        logger.info(f"State saved for {filename}")

    # 全ファイル処理後、リリースノートを更新
    logger.info(f"Updating release notes for {tag}")
    body = release_body(device_name, state[tag])
    with tempfile.NamedTemporaryFile('w', delete=False, suffix='.md') as f:
        f.write(body)
        notes_file = f.name
    subprocess.run(['gh', 'release', 'edit', tag, '--notes-file', notes_file], check=True)
    os.unlink(notes_file)
    logger.info(f"Release notes updated for {tag}")


def main():
    # 状態読み込み
    state = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            state = json.load(f)
    logger.info(f"Loaded state with {len(state)} devices")

    devices = fetch_and_parse()

    for tag, (device_name, entries) in devices.items():
        logger.info(f"\n=== Processing device: {device_name} (tag: {tag}) ===")
        process_device(tag, device_name, entries, state)

    logger.info("All devices processed.")


if __name__ == '__main__':
    main()
