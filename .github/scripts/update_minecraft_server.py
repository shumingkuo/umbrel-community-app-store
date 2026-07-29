import json
import os
import re
import time

import requests


def log(message):
    print(message, flush=True)


def resolve_app_dir():
    env_app_dir = os.environ.get("APP_DIR")
    if env_app_dir:
        return env_app_dir

    if os.path.exists("docker-compose.yml") and os.path.exists("umbrel-app.yml"):
        return "."

    if os.path.exists("hsukuo-minecraft/docker-compose.yml") and os.path.exists("hsukuo-minecraft/umbrel-app.yml"):
        return "hsukuo-minecraft"

    raise FileNotFoundError(
        "Could not determine app directory. Set APP_DIR explicitly, for example APP_DIR=. or APP_DIR=hsukuo-minecraft"
    )


def get_output_path(filename):
    base_dir = os.environ.get("RUNNER_TEMP", ".")
    return os.path.join(base_dir, filename)


APP_DIR = resolve_app_dir()
DOCKER_COMPOSE_PATH = os.path.join(APP_DIR, "docker-compose.yml")
UMBREL_APP_PATH = os.path.join(APP_DIR, "umbrel-app.yml")

ITZG_IMAGE_REPO = "itzg/minecraft-server"
ITZG_IMAGE_TAG = "java25"
VIAVERSION_REPO = "ViaVersion/ViaVersion"

MINECRAFT_SERVER_DOWNLOAD_URL = "https://www.minecraft.net/en-us/download/server"
MINECRAFT_VERSION_MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"


def read_text(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def write_text(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def ensure_file_exists(path):
    if not os.path.exists(path):
        raise FileNotFoundError(f"Required file not found: {path}")


def http_get(url, retries=3, timeout=(10, 20), **kwargs):
    headers = kwargs.pop("headers", {})
    headers.setdefault("User-Agent", "github-actions-minecraft-updater")
    last_error = None

    for attempt in range(1, retries + 1):
        try:
            log(f"[http] GET {url} (attempt {attempt}/{retries})")
            resp = requests.get(url, headers=headers, timeout=timeout, **kwargs)
            resp.raise_for_status()
            log(f"[http] OK  {url} -> {resp.status_code}")
            return resp
        except requests.RequestException as e:
            last_error = e
            log(f"[http] ERR {url}: {e}")
            if attempt < retries:
                sleep_seconds = attempt * 2
                log(f"[http] retry after {sleep_seconds}s")
                time.sleep(sleep_seconds)
            else:
                raise last_error


def get_latest_viaversion():
    log("[step] fetching latest ViaVersion release")
    url = f"https://api.github.com/repos/{VIAVERSION_REPO}/releases/latest"
    data = http_get(url).json()
    version = data["tag_name"].lstrip("v")
    log(f"[data] latest ViaVersion = {version}")
    return version


def get_latest_minecraft_server_version():
    log("[step] fetching latest Minecraft version from Mojang manifest")
    try:
        data = http_get(MINECRAFT_VERSION_MANIFEST_URL).json()
        latest_release = data.get("latest", {}).get("release")
        if latest_release:
            log(f"[data] latest Minecraft version = {latest_release} (from manifest)")
            return latest_release
    except requests.RequestException as e:
        log(f"[warn] manifest fetch failed: {e}")

    log("[step] fallback to Minecraft download page")
    html = http_get(MINECRAFT_SERVER_DOWNLOAD_URL).text
    patterns = [
        r'minecraft_server\.(\d+\.\d+(?:\.\d+)?)\.jar',
        r'downloads/minecraft_server\.(\d+\.\d+(?:\.\d+)?)\.jar',
    ]
    for pattern in patterns:
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            version = m.group(1)
            log(f"[data] latest Minecraft version = {version} (from download page)")
            return version

    raise RuntimeError("Could not determine latest Minecraft release version")


def get_dockerhub_bearer_token(repo):
    log(f"[step] fetching Docker Hub token for {repo}")
    url = "https://auth.docker.io/token"
    params = {
        "service": "registry.docker.io",
        "scope": f"repository:{repo}:pull",
    }
    data = http_get(url, params=params).json()
    return data["token"]


def get_latest_image_digest(repo, tag):
    log(f"[step] fetching image digest for {repo}:{tag}")
    token = get_dockerhub_bearer_token(repo)
    url = f"https://registry-1.docker.io/v2/{repo}/manifests/{tag}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": ",".join([
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
            "application/vnd.docker.distribution.manifest.v2+json",
        ]),
    }
    resp = http_get(url, headers=headers)
    digest = resp.headers.get("Docker-Content-Digest")
    if not digest:
        raise RuntimeError("Docker digest not found in registry response")
    log(f"[data] latest image digest = {digest}")
    return digest


def parse_umbrel_version(version_str):
    match = re.fullmatch(r"(\d{4})\.(\d{1,2})\.(\d+)", version_str.strip())
    if not match:
        raise RuntimeError(f"Invalid umbrel app version format: {version_str!r}")
    year, month, patch = match.groups()
    return int(year), int(month), int(patch)


def bump_umbrel_version(current_version, now):
    current_year, current_month, current_patch = parse_umbrel_version(current_version)
    target_year = now.tm_year
    target_month = now.tm_mon

    if current_year == target_year and current_month == target_month:
        next_patch = current_patch + 1
    else:
        next_patch = 0

    return f"{target_year}.{target_month}.{next_patch}"


def replace_umbrel_version(text, new_version):
    updated_text, count = re.subn(
        r'(^version:\s*")([^"]+)(")',
        rf'\g<1>{new_version}\g<3>',
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if count != 1:
        raise RuntimeError("Could not find umbrel-app.yml version field")
    return updated_text


def format_release_notes(changes):
    lines = ["This update includes:"]

    if "minecraft_version" in changes:
        old, new = changes["minecraft_version"]
        lines.append(f"  - Update Minecraft server version from {old} to {new}")

    if "viaversion" in changes:
        old, new = changes["viaversion"]
        lines.append(f"  - Update ViaVersion plugin from {old} to {new}")

    if "image_digest" in changes:
        old, new = changes["image_digest"]
        lines.append(f"  - Refresh `itzg/minecraft-server:{ITZG_IMAGE_TAG}` image digest")
        lines.append(f"  - Previous digest: `{old}`")
        lines.append(f"  - New digest: `{new}`")

    return lines


def replace_release_notes_block(text, release_note_lines):
    lines = text.splitlines()
    start_idx = None

    for i, line in enumerate(lines):
        if re.match(r"^releaseNotes:\s*>-\s*$", line):
            start_idx = i
            break

    if start_idx is None:
        raise RuntimeError("Could not find releaseNotes block in umbrel-app.yml")

    end_idx = len(lines)
    for i in range(start_idx + 1, len(lines)):
        line = lines[i]
        if line and not line.startswith(" "):
            end_idx = i
            break

    new_block = ["releaseNotes: >-"]
    for item in release_note_lines:
        new_block.append(f"  {item}" if item else "")

    updated_lines = lines[:start_idx] + new_block + lines[end_idx:]
    return "\n".join(updated_lines) + "\n"


def build_pr_title(changes):
    title_parts = []
    if "minecraft_version" in changes:
        title_parts.append(f"Minecraft {changes['minecraft_version'][1]}")
    if "viaversion" in changes:
        title_parts.append(f"ViaVersion {changes['viaversion'][1]}")
    if "image_digest" in changes:
        title_parts.append("image refresh")
    return "Update Minecraft app: " + ", ".join(title_parts)


def build_pr_body(release_notes_lines):
    return "\n".join([
        "## Summary",
        "",
        *release_notes_lines,
        "",
        "## Files updated",
        "",
        f"- `{DOCKER_COMPOSE_PATH}`",
        f"- `{UMBREL_APP_PATH}`",
        "",
        "## Automation",
        "",
        "- This PR was created automatically by the scheduled Minecraft app updater workflow.",
    ])


def main():
    log(f"[info] APP_DIR={APP_DIR}")
    log(f"[info] DOCKER_COMPOSE_PATH={DOCKER_COMPOSE_PATH}")
    log(f"[info] UMBREL_APP_PATH={UMBREL_APP_PATH}")

    ensure_file_exists(DOCKER_COMPOSE_PATH)
    ensure_file_exists(UMBREL_APP_PATH)

    log("[step] reading files")
    compose_text = read_text(DOCKER_COMPOSE_PATH)
    umbrel_text = read_text(UMBREL_APP_PATH)

    log("[step] parsing current values")
    current_image_match = re.search(
        r"(image:\s*itzg/minecraft-server:java25@)(sha256:[a-f0-9]+)",
        compose_text,
    )
    if not current_image_match:
        raise RuntimeError("Could not find current Docker image digest")
    current_digest = current_image_match.group(2)
    log(f"[data] current image digest = {current_digest}")

    current_mc_match = re.search(
        r'(^\s*VERSION:\s*")([^"]+)(")',
        compose_text,
        re.MULTILINE,
    )
    if not current_mc_match:
        raise RuntimeError("Could not find current VERSION")
    current_mc_version = current_mc_match.group(2)
    log(f"[data] current Minecraft version = {current_mc_version}")

    current_vv_match = re.search(
        r"https://github\.com/ViaVersion/ViaVersion/releases/download/([^/]+)/ViaVersion-([0-9][^/]+?)\.jar",
        compose_text,
    )
    if not current_vv_match:
        raise RuntimeError("Could not find ViaVersion plugin URL")
    current_vv_version = current_vv_match.group(2)
    log(f"[data] current ViaVersion = {current_vv_version}")

    current_app_version_match = re.search(
        r'^version:\s*"([^"]+)"$',
        umbrel_text,
        re.MULTILINE,
    )
    if not current_app_version_match:
        raise RuntimeError("Could not find current umbrel app version")
    current_app_version = current_app_version_match.group(1)
    log(f"[data] current umbrel app version = {current_app_version}")

    latest_vv_version = get_latest_viaversion()
    latest_mc_version = get_latest_minecraft_server_version()
    latest_digest = get_latest_image_digest(ITZG_IMAGE_REPO, ITZG_IMAGE_TAG)

    log("[step] comparing values")
    updated_compose = compose_text
    changes = {}

    if current_vv_version != latest_vv_version:
        updated_compose = re.sub(
            r"https://github\.com/ViaVersion/ViaVersion/releases/download/[^/]+/ViaVersion-[^/]+\.jar",
            f"https://github.com/ViaVersion/ViaVersion/releases/download/{latest_vv_version}/ViaVersion-{latest_vv_version}.jar",
            updated_compose,
        )
        changes["viaversion"] = (current_vv_version, latest_vv_version)
        log(f"[change] ViaVersion: {current_vv_version} -> {latest_vv_version}")

    if current_mc_version != latest_mc_version:
        updated_compose = re.sub(
            r'(^\s*VERSION:\s*")([^"]+)(")',
            rf'\g<1>{latest_mc_version}\g<3>',
            updated_compose,
            flags=re.MULTILINE,
        )
        changes["minecraft_version"] = (current_mc_version, latest_mc_version)
        log(f"[change] Minecraft version: {current_mc_version} -> {latest_mc_version}")

    if current_digest != latest_digest:
        updated_compose = re.sub(
            r"(image:\s*itzg/minecraft-server:java25@)sha256:[a-f0-9]+",
            rf"\g<1>{latest_digest}",
            updated_compose,
        )
        changes["image_digest"] = (current_digest, latest_digest)
        log("[change] Docker image digest updated")

    if not changes:
        log("[done] No changes detected.")
        return

    now = time.localtime()
    new_app_version = bump_umbrel_version(current_app_version, now)
    log(f"[change] umbrel app version: {current_app_version} -> {new_app_version}")

    log("[step] writing docker-compose.yml")
    write_text(DOCKER_COMPOSE_PATH, updated_compose)

    release_notes_lines = format_release_notes(changes)

    log("[step] writing umbrel-app.yml version and releaseNotes")
    updated_umbrel = replace_umbrel_version(umbrel_text, new_app_version)
    updated_umbrel = replace_release_notes_block(updated_umbrel, release_notes_lines)
    write_text(UMBREL_APP_PATH, updated_umbrel)

    pr_title = build_pr_title(changes)
    pr_body = build_pr_body(release_notes_lines)

    log(f"[data] pr_title = {pr_title}")

    write_text(get_output_path("minecraft-pr-title"), pr_title)
    write_text(get_output_path("minecraft-pr-body"), pr_body)

    log("[done] Update completed")
    print(json.dumps({
        "changed": True,
        "app_dir": APP_DIR,
        "pr_title": pr_title,
        "changes": changes,
        "umbrel_app_version": new_app_version,
    }, indent=2))


if __name__ == "__main__":
    main()