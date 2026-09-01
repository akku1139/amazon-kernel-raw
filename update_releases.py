#!/usr/bin/env python3
import json
import logging
import os
import re
import subprocess
import tempfile
import hashlib
from pathlib import Path
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

SOURCE_URL = "https://digprjsurvey.amazon.com/csad/help/node/200203720"
STATE_FILE = "releases_state.json"
PREFIX = "ks-"
TAG_MAX_LEN = 40
ASSET_DIR = Path(tempfile.gettempdir()) / "kindle_src_assets"
MAX_ASSET_SIZE = 1900 * 1024 * 1024  # 1.9GB
MAX_WORKERS = 10

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# スレッド間共有ロック
state_lock = threading.RLock()   # 再入可能ロック
git_lock = threading.Lock()


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
    path = Path(filename)
    suffixes = path.suffixes
    base = path.name
    if suffixes:
        last_ext = suffixes[-1]
        base = base[:-len(last_ext)]
        if base.endswith('.tar'):
            base = base[:-4]

    m = re.search(r'(\d+(?:\.\d+)+)[-_](\d+)', base)
    if m:
        return m.group(1), m.group(2)

    m = re.search(r'(\d+(?:\.\d+)+)', base)
    if m:
        return m.group(1), ''

    return '', ''


def fetch_and_parse() -> dict:
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
                'uploaded': False,
                'assets': [],
                'error': None
            })

        if entries:
            tag = make_tag(device_name)
            devices[tag] = (device_name, entries)

    logger.info(f"Found {len(devices)} devices")
    return devices


def release_body(device_name: str, entries: list) -> str:
    lines = [
        f"# Kindle Source Code Releases - {device_name}",
        "",
        "Automatically updated from [Amazon Source Code Notice](https://digprjsurvey.amazon.com/csad/help/node/200203720).",
        "",
        "## Sources",
        "",
        "| Version | Build | Asset(s) | Source URL |",
        "|---------|-------|----------|------------|"
    ]

    sorted_entries = sorted(entries, key=version_key, reverse=True)
    tag = make_tag(device_name)
    for e in sorted_entries:
        version = e.get('version') or 'N/A'
        build = e.get('build') or 'N/A'
        filename = e['filename']
        url = e['url']

        asset_links = []
        assets = e.get('assets') or []
        if e.get('uploaded', False) and assets:
            for asset_name in assets:
                link = f"[{asset_name}](../../releases/download/{tag}/{asset_name})"
                asset_links.append(link)
        elif not e.get('uploaded', False) and e.get('error'):
            asset_links.append(f"Not uploaded ({e['error']})")
        else:
            asset_links.append("Not uploaded")

        asset_str = ", ".join(asset_links)
        source_link = f"[{filename}]({url})"
        lines.append(f"| {version} | {build} | {asset_str} | {source_link} |")

    return "\n".join(lines)


def get_existing_assets(tag: str) -> set:
    """指定タグのリリースに存在するアセット名のセットを返す（キャッシュ用）"""
    result = subprocess.run(
        ['gh', 'release', 'view', tag, '--json', 'assets', '--jq', '.assets[].name'],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return set()
    return set(result.stdout.splitlines())


def download_file(url: str, dest: Path):
    logger.info(f"Downloading {url}")
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dest, 'wb') as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
    logger.info(f"Downloaded to {dest}")


def split_file(src: Path, dest_dir: Path, part_prefix: str) -> list:
    logger.info(f"Splitting {src.name} into parts (max {MAX_ASSET_SIZE} bytes)")
    dest_dir.mkdir(exist_ok=True)
    cmd = ['split', '-b', str(MAX_ASSET_SIZE), '-d', '-a', '2', str(src), str(dest_dir / part_prefix)]
    subprocess.run(cmd, check=True)
    parts = sorted(dest_dir.glob(f"{part_prefix}*"))
    logger.info(f"Split into {len(parts)} parts")
    return parts


def upload_file(tag: str, local_path: Path):
    logger.info(f"Uploading {local_path.name} to {tag}")
    subprocess.run(['gh', 'release', 'upload', tag, str(local_path)], check=True)
    logger.info(f"Uploaded {local_path.name}")


def save_state_and_push(state: dict, commit_msg: str):
    """
    状態をファイルに保存し、git commit/pushする。
    state_lock (RLock) を内部で取得し、git_lockでgit操作を直列化する。
    """
    with state_lock:
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2)
    with git_lock:
        subprocess.run(['git', 'add', STATE_FILE], check=True)
        diff = subprocess.run(['git', 'diff', '--cached', '--quiet'], capture_output=True)
        if diff.returncode != 0:
            subprocess.run(['git', 'commit', '-m', commit_msg], check=True)
            subprocess.run(['git', 'push'], check=True)
            logger.info(f"State committed: {commit_msg}")
        else:
            logger.debug("No state changes to commit")


def process_device(tag: str, device_name: str, current_entries: list, state: dict):
    existing = state.get(tag, [])
    existing_filenames = {e['filename'] for e in existing}

    new_entries = [e for e in current_entries if e['filename'] not in existing_filenames]
    if not new_entries:
        logger.info(f"No new entries for {tag}, skipping.")
        return

    logger.info(f"Processing {device_name} (tag: {tag}), {len(new_entries)} new file(s)")

    # リリース存在確認と作成
    view_result = subprocess.run(['gh', 'release', 'view', tag], capture_output=True, text=True)
    release_exists = view_result.returncode == 0

    if not release_exists:
        logger.info(f"Creating release {tag}")
        with tempfile.NamedTemporaryFile('w', delete=False, suffix='.md') as f:
            f.write(f"# {device_name}\n\nInitializing...")
            notes_file = f.name
        subprocess.run(['gh', 'release', 'create', tag,
                        '--title', device_name,
                        '--notes-file', notes_file], check=True)
        os.unlink(notes_file)

    ASSET_DIR.mkdir(exist_ok=True)

    # リリースの既存アセット一覧をキャッシュ
    existing_assets = get_existing_assets(tag)
    logger.info(f"Fetched {len(existing_assets)} existing assets for {tag}")

    for entry in new_entries:
        filename = entry['filename']
        logger.info(f"--- Processing file: {filename} ---")

        # 事前チェック：既にアセットが存在するか（分割前のファイル名で）
        if filename in existing_assets:
            logger.info(f"Asset {filename} already exists (pre-check), skipping download.")
            entry['uploaded'] = True
            entry['assets'] = [filename]
            existing.append(entry)
            # 状態更新＆コミット
            with state_lock:
                state[tag] = sorted(existing, key=version_key, reverse=True)
            save_state_and_push(state, f"Add {filename} to {tag} (already exists)")
            continue

        # ダウンロード
        local_path = ASSET_DIR / filename
        try:
            download_file(entry['url'], local_path)
        except Exception as e:
            logger.error(f"Download failed for {filename}: {e}")
            entry['uploaded'] = False
            entry['error'] = f"Download error: {e}"
            existing.append(entry)
            with state_lock:
                state[tag] = sorted(existing, key=version_key, reverse=True)
            save_state_and_push(state, f"Mark {filename} as download failed")
            continue

        # サイズ確認
        size = local_path.stat().st_size
        if size > MAX_ASSET_SIZE:
            # 分割が必要
            logger.info(f"File size {size} exceeds limit, splitting...")
            split_prefix = filename + ".part"
            try:
                parts = split_file(local_path, ASSET_DIR, split_prefix)
                uploaded_names = []
                for part in parts:
                    part_name = part.name
                    if part_name in existing_assets:
                        logger.info(f"Part {part_name} already exists, skipping upload.")
                    else:
                        upload_file(tag, part)
                        existing_assets.add(part_name)
                    uploaded_names.append(part_name)
                entry['uploaded'] = True
                entry['assets'] = uploaded_names
                entry['error'] = None
            except Exception as e:
                logger.error(f"Split/upload failed for {filename}: {e}")
                entry['uploaded'] = False
                entry['error'] = f"Split/upload error: {e}"
            finally:
                if local_path.exists():
                    local_path.unlink()
                for part in ASSET_DIR.glob(split_prefix + "*"):
                    part.unlink()
        else:
            # そのままアップロード
            if filename in existing_assets:
                logger.info(f"Asset {filename} already exists, skipping upload.")
                entry['uploaded'] = True
                entry['assets'] = [filename]
            else:
                try:
                    upload_file(tag, local_path)
                    existing_assets.add(filename)
                    entry['uploaded'] = True
                    entry['assets'] = [filename]
                except subprocess.CalledProcessError as e:
                    logger.error(f"Upload failed for {filename}: {e}")
                    entry['uploaded'] = False
                    entry['error'] = f"Upload error: {e.stderr.strip() if e.stderr else 'unknown'}"
            if local_path.exists():
                local_path.unlink()

        # 状態更新＆コミット（ファイル単位）
        existing.append(entry)
        with state_lock:
            state[tag] = sorted(existing, key=version_key, reverse=True)
        save_state_and_push(state, f"Process {filename} for {tag}")

    # リリースノート更新
    logger.info(f"Updating release notes for {tag}")
    body = release_body(device_name, state[tag])
    with tempfile.NamedTemporaryFile('w', delete=False, suffix='.md') as f:
        f.write(body)
        notes_file = f.name
    subprocess.run(['gh', 'release', 'edit', tag, '--notes-file', notes_file], check=True)
    os.unlink(notes_file)
    logger.info(f"Release notes updated for {tag}")


def main():
    state = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            state = json.load(f)
    logger.info(f"Loaded state with {len(state)} devices")

    devices = fetch_and_parse()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = []
        for tag, (device_name, entries) in devices.items():
            future = executor.submit(process_device, tag, device_name, entries, state)
            futures.append(future)

        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                logger.error(f"Device processing failed: {e}")

    logger.info("All devices processed.")


if __name__ == '__main__':
    main()
