#!/usr/bin/env python3
import hashlib
import html
import json
import os
import re
import shutil
import signal
import subprocess
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("OWB_DATA", APP_DIR / "data"))
LOCALE_DIR = APP_DIR / "locales"
DOWNLOAD_DIR = DATA_DIR / "downloads"
BUILD_DIR = DATA_DIR / "builders"
OUTPUT_DIR = DATA_DIR / "firmware"
LOG_DIR = DATA_DIR / "logs"
CONFIG_PATH = DATA_DIR / "config.json"
STATE_PATH = DATA_DIR / "state.json"
REPO_FULL_NAME = os.environ.get("OWB_REPO", "nick2ld/openwrt-builder")
REPO_URL = f"https://github.com/{REPO_FULL_NAME}"
DEVICE_CACHE = {}
RELEASE_CACHE_TTL = 3600
HTTP_TIMEOUT = int(os.environ.get("OWB_HTTP_TIMEOUT", "20"))
VERSION_CHECK_TIMEOUT = int(os.environ.get("OWB_VERSION_CHECK_TIMEOUT", "30"))
BUILD_LOCK = threading.Lock()
ACTIVE_JOBS = {}
ACTIVE_JOBS_LOCK = threading.Lock()
CANCELLED_JOBS = set()

DEFAULT_CONFIG = {
    "listen_host": "0.0.0.0",
    "listen_port": 8088,
    "public_base_url": "http://openwrt-builder.lan:8088",
    "release_branch_prefix": "25.",
    "check_interval_minutes": 360,
    "build_threads": 1,
    "keep_builders": 2,
    "allow_untrusted_apk": True,
    "routers": [],
    "package_sources": [],
}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_dirs():
    for path in [DATA_DIR, DOWNLOAD_DIR, BUILD_DIR, OUTPUT_DIR, LOG_DIR]:
        path.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        write_json(CONFIG_PATH, DEFAULT_CONFIG)
    if not STATE_PATH.exists():
        write_json(STATE_PATH, {"last_check": None, "latest_release": None, "jobs": []})


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        return default


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def read_text_file(path):
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


def config():
    cfg = DEFAULT_CONFIG.copy()
    cfg.update(read_json(CONFIG_PATH, DEFAULT_CONFIG))
    return cfg


def state():
    return read_json(STATE_PATH, {"last_check": None, "latest_release": None, "jobs": []})


def job_progress(status):
    return {
        "queued": 0,
        "checking": 8,
        "waiting_apks": 35,
        "running": 18,
        "downloading": 25,
        "building": 70,
        "publishing": 92,
        "success": 100,
        "skipped": 100,
        "failed": 100,
        "cancelled": 100,
    }.get(status, 0)


def last_log_line(log_url):
    if not log_url:
        return ""
    path = LOG_DIR / Path(str(log_url)).name
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            last = ""
            for line in fh:
                if line.strip():
                    last = line.strip()
            return last
    except FileNotFoundError:
        return ""


def job_by_log_name(log_name):
    for job in state().get("jobs", []):
        if Path(str(job.get("log", ""))).name == log_name:
            return job
    return None


def read_log_response(file_path):
    text = file_path.read_text(encoding="utf-8", errors="replace")
    job = job_by_log_name(file_path.name)
    if job and job.get("status") == "success" and job.get("output") and "Build completed successfully" not in text:
        text = text.rstrip() + f"\n[{job.get('updated_at') or utc_now()}] Firmware ready: {job.get('output')}\n"
        text += f"[{job.get('updated_at') or utc_now()}] Build completed successfully\n"
    return text


def enriched_state():
    st = state()
    cfg = config()
    jobs = []
    for job in st.get("jobs", []):
        item = dict(job)
        item.setdefault("progress", job_progress(item.get("status")))
        item["last_line"] = item.get("last_line") or last_log_line(item.get("log", ""))
        jobs.append(item)
    st["jobs"] = jobs
    st["routers_status"] = router_statuses(cfg, st)
    return st


def update_state(mutator):
    current = state()
    mutator(current)
    write_json(STATE_PATH, current)
    return current


def http_get(url, timeout=None):
    if timeout is None:
        timeout = HTTP_TIMEOUT
    req = urllib.request.Request(url, headers={"User-Agent": "local-openwrt-builder/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(), dict(resp.headers)


def http_text(url, timeout=None):
    data, _ = http_get(url, timeout=timeout)
    return data.decode("utf-8", errors="replace")


def http_url_exists(url, timeout=None):
    if timeout is None:
        timeout = HTTP_TIMEOUT
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "local-openwrt-builder/1.0"}, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except urllib.error.HTTPError as exc:
        if exc.code not in [403, 405]:
            return False
    except Exception:
        return False
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "local-openwrt-builder/1.0", "Range": "bytes=0-0"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except Exception:
        return False


def parse_links(index_html):
    return [html.unescape(m) for m in re.findall(r'href=["\']([^"\']+)["\']', index_html, flags=re.I)]


def normalize_base_url(url):
    if url.endswith(".apk"):
        return url
    return url.rstrip("/") + "/"


def version_key(value):
    parts = re.split(r"([0-9]+)", value)
    return [int(p) if p.isdigit() else p for p in parts]


def latest_openwrt_release(branch_prefix, timeout=None, allow_stale_cache=True):
    cache_key = ("latest_release", branch_prefix)
    cached = DEVICE_CACHE.get(cache_key)
    now = time.time()
    if cached and now - cached.get("ts", 0) < RELEASE_CACHE_TTL:
        return cached["release"]
    try:
        text = http_text("https://downloads.openwrt.org/releases/", timeout=timeout)
        versions = []
        for link in parse_links(text):
            name = link.strip("/")
            if name.startswith(branch_prefix) and re.match(r"^\d+\.\d+(?:\.\d+)?$", name):
                versions.append(name)
        if not versions:
            raise RuntimeError(f"No OpenWrt releases found for prefix {branch_prefix!r}")
        release = sorted(versions, key=version_key)[-1]
        DEVICE_CACHE[cache_key] = {"release": release, "ts": now}
        return release
    except Exception:
        if allow_stale_cache and cached:
            return cached["release"]
        st = state()
        fallback = st.get("latest_release") or st.get("built_release")
        if allow_stale_cache and fallback:
            return fallback
        raise


def title_to_text(title):
    if isinstance(title, str):
        return title
    if not isinstance(title, dict):
        return ""
    parts = []
    for key in ["vendor", "model", "variant", "version"]:
        value = str(title.get(key, "")).strip()
        if value:
            parts.append(value)
    return " ".join(parts)


def profile_display_name(profile_id, profile):
    titles = profile.get("titles") or []
    names = [title_to_text(item) for item in titles]
    names = [name for name in names if name]
    if names:
        return " / ".join(names)
    return profile_id.replace("_", " ").replace(",", " ")


def merge_packages(*groups):
    result = []
    seen = set()
    for group in groups:
        for package in split_packages(group):
            if package not in seen:
                seen.add(package)
                result.append(package)
    return result


def split_target_path(target_path):
    parts = str(target_path or "").strip("/").split("/")
    if len(parts) >= 2:
        return parts[0], parts[1]
    return str(target_path or ""), ""


def load_release_overview(release):
    cache_key = ("overview", release)
    cached = DEVICE_CACHE.get(cache_key)
    if cached:
        return cached
    url = f"https://downloads.openwrt.org/releases/{release}/.overview.json"
    data = json.loads(http_text(url))
    DEVICE_CACHE[cache_key] = data
    return data


def load_profiles_json(release, target_path):
    cache_key = ("profiles", release, target_path)
    cached = DEVICE_CACHE.get(cache_key)
    if cached:
        return cached
    target, subtarget = split_target_path(target_path)
    url = f"https://downloads.openwrt.org/releases/{release}/targets/{target}/{subtarget}/profiles.json"
    data = json.loads(http_text(url))
    DEVICE_CACHE[cache_key] = data
    return data


def search_devices(query, limit=20):
    cfg = config()
    try:
        release = latest_openwrt_release(cfg.get("release_branch_prefix", "25."), timeout=10, allow_stale_cache=True)
        overview = load_release_overview(release)
    except Exception as exc:
        return {
            "release": None,
            "devices": [],
            "error": f"Cannot load OpenWrt device index: {exc}",
        }
    words = [w.lower() for w in re.split(r"\s+", query.strip()) if w.strip()]
    if not words:
        return {"release": release, "devices": []}
    matches = []
    for item in overview.get("profiles", []):
        profile_id = item.get("id") or item.get("profile") or ""
        target_path = item.get("target") or ""
        target, subtarget = split_target_path(target_path)
        titles = item.get("titles") or []
        if isinstance(titles, dict):
            titles = [titles]
        name = " / ".join([title_to_text(t) for t in titles if title_to_text(t)]) or profile_id
        haystack = f"{name} {profile_id} {target_path}".lower()
        if all(word in haystack for word in words):
            matches.append({
                "name": name,
                "profile": profile_id,
                "target": target,
                "subtarget": subtarget,
                "target_path": target_path,
                "arch": "",
                "packages": "",
            })
        if len(matches) >= limit:
            break
    for match in matches:
        try:
            profiles_json = load_profiles_json(release, match["target_path"])
            profile = profiles_json.get("profiles", {}).get(match["profile"], {})
            packages = merge_packages(
                profiles_json.get("default_packages", []),
                profiles_json.get("target_packages", []),
                profile.get("device_packages", []),
                profile.get("packages", []),
                ["luci", "luci-app-attendedsysupgrade"],
            )
            match["arch"] = profiles_json.get("arch_packages", "")
            match["packages"] = " ".join(packages)
            match["name"] = profile_display_name(match["profile"], profile)
        except Exception:
            pass
    return {"release": release, "devices": matches}


def installed_version_info():
    return {
        "repo": REPO_FULL_NAME,
        "repo_url": REPO_URL,
        "branch": read_text_file(APP_DIR / "VERSION") or "main",
        "commit": read_text_file(APP_DIR / "COMMIT"),
    }


def latest_repo_commit(branch="main"):
    errors = []
    urls = [
        f"https://api.github.com/repos/{REPO_FULL_NAME}/git/ref/heads/{branch}",
        f"https://api.github.com/repos/{REPO_FULL_NAME}/commits/{branch}",
    ]
    for url in urls:
        try:
            data = json.loads(http_text(url, timeout=VERSION_CHECK_TIMEOUT))
            sha = data.get("object", {}).get("sha") or data.get("sha", "")
            if sha:
                return sha
        except Exception as exc:
            errors.append(f"{urllib.parse.urlparse(url).netloc}: {exc}")

    try:
        proc = subprocess.run(
            ["git", "ls-remote", REPO_URL + ".git", f"refs/heads/{branch}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=VERSION_CHECK_TIMEOUT,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.split()[0]
        if proc.stderr.strip():
            errors.append(proc.stderr.strip().splitlines()[-1])
    except Exception as exc:
        errors.append(f"git ls-remote: {exc}")

    raise RuntimeError("; ".join(errors) or "cannot check latest commit")


def version_status():
    info = installed_version_info()
    latest = ""
    error = ""
    try:
        latest = latest_repo_commit("main")
        def mutate(st):
            st["latest_app_commit"] = latest
            st["latest_app_commit_checked_at"] = utc_now()
        update_state(mutate)
    except Exception as exc:
        error = str(exc)
        latest = state().get("latest_app_commit", "")
    current = info.get("commit") or ""
    return {
        **info,
        "latest_commit": latest,
        "update_available": bool(current and latest and current != latest),
        "current_short": current[:7] if current else "",
        "latest_short": latest[:7] if latest else "",
        "error": error,
        "latest_cached": bool(error and latest),
        "latest_checked_at": state().get("latest_app_commit_checked_at", ""),
    }


def run_self_update():
    log_path = LOG_DIR / "self-update.log"
    cmd = ["sudo", "-n", "/usr/local/sbin/openwrt-builder-update"]
    with log_path.open("a", encoding="utf-8") as logfh:
        logfh.write(f"[{utc_now()}] Running self update\n")
        logfh.flush()
        try:
            proc = subprocess.Popen(cmd, stdout=logfh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        except Exception as exc:
            logfh.write(f"[{utc_now()}] ERROR: updater helper could not start: {exc}\n")
            raise RuntimeError(f"updater helper could not start: {exc}")
        for _ in range(10):
            if proc.poll() is not None:
                break
            time.sleep(0.2)
        if proc.poll() is not None and proc.returncode != 0:
            logfh.write(f"[{utc_now()}] ERROR: updater helper failed with exit code {proc.returncode}\n")
            raise RuntimeError(f"updater helper failed with exit code {proc.returncode}")
        if proc.poll() is None:
            logfh.write(f"[{utc_now()}] Updater helper is still running in background\n")

    def mutate(st):
        st["self_update"] = {"started_at": utc_now(), "log": "/logs/self-update.log"}
    update_state(mutate)
    return {"status": "started", "log": "/logs/self-update.log"}


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_file(url, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "local-openwrt-builder/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp, tmp.open("wb") as out:
        shutil.copyfileobj(resp, out)
    tmp.replace(dest)
    return dest


def imagebuilder_url(release, target, subtarget):
    base = f"https://downloads.openwrt.org/releases/{release}/targets/{target}/{subtarget}/"
    filename = f"openwrt-imagebuilder-{release}-{target}-{subtarget}.Linux-x86_64.tar.zst"
    return base + filename


def verify_download_sha256(release, target, subtarget, file_path):
    sums_url = f"https://downloads.openwrt.org/releases/{release}/targets/{target}/{subtarget}/sha256sums"
    text = http_text(sums_url)
    name = file_path.name
    for line in text.splitlines():
        if name in line:
            expected = line.split()[0]
            actual = sha256_file(file_path)
            if expected != actual:
                raise RuntimeError(f"SHA256 mismatch for {name}: expected {expected}, got {actual}")
            return True
    return False


def ensure_imagebuilder(release, router, log):
    target = router["target"]
    subtarget = router["subtarget"]
    archive = DOWNLOAD_DIR / "imagebuilders" / release / f"{target}-{subtarget}.tar.zst"
    extracted = BUILD_DIR / release / f"{target}-{subtarget}"
    marker = extracted / ".ready"
    if marker.exists():
        return extracted
    if not archive.exists():
        url = imagebuilder_url(release, target, subtarget)
        log(f"Downloading ImageBuilder: {url}")
        download_file(url, archive)
        verify_download_sha256(release, target, subtarget, archive)
    if extracted.exists():
        shutil.rmtree(extracted)
    extracted.parent.mkdir(parents=True, exist_ok=True)
    log(f"Extracting ImageBuilder into {extracted}")
    subprocess.run(["tar", "--zstd", "-xf", str(archive), "-C", str(extracted.parent)], check=True)
    candidates = [p for p in extracted.parent.iterdir() if p.is_dir() and "imagebuilder" in p.name.lower()]
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    if newest != extracted:
        if extracted.exists():
            shutil.rmtree(extracted)
        newest.rename(extracted)
    marker.write_text(utc_now(), encoding="utf-8")
    return extracted


def package_name_from_apk(filename):
    name = filename.rsplit("/", 1)[-1]
    if not name.endswith(".apk"):
        return ""
    stem = name[:-4]
    if "_" in stem:
        return stem.split("_", 1)[0]
    version_match = re.match(r"^(.+)-(?:v)?\d[\w.+:~]*?(?:-r\d+)?$", stem)
    if version_match:
        return version_match.group(1)
    return stem


def source_candidate_urls(src, release, arch):
    raw = src.get("url", "").strip()
    if not raw:
        return []
    templated = raw.format(release=release, arch=arch)
    if templated.endswith(".apk"):
        return [templated]
    base = normalize_base_url(templated)
    candidates = [base]
    if "{arch}" not in raw and arch:
        candidates.append(urllib.parse.urljoin(base, arch + "/"))
    if "{release}" not in raw:
        candidates.append(urllib.parse.urljoin(base, release + "/"))
        if arch:
            candidates.append(urllib.parse.urljoin(base, release + "/" + arch + "/"))
    seen = []
    for item in candidates:
        if item not in seen:
            seen.append(item)
    return seen


def package_requested(filename, requested):
    if not requested:
        return True
    package = package_name_from_apk(filename)
    return package in requested or any(filename.startswith(name + "_") or filename.startswith(name + "-") for name in requested)


def apk_matches_platform(filename, arch, target="", subtarget=""):
    if not arch:
        arch_ok = True
    else:
        arch_ok = filename.endswith(f"_{arch}.apk") or f"_{arch}_" in filename or "_all.apk" in filename
    if not arch_ok:
        return False
    if target and subtarget:
        platform = f"_{target}_{subtarget}.apk"
        return filename.endswith(platform) or filename.endswith(f"_{arch}.apk") or filename.endswith("_all.apk")
    return True


def github_repo_from_url(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc.lower() != "github.com":
        return None
    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return None


def github_release_assets(release_obj, log):
    assets_url = release_obj.get("assets_url")
    if not assets_url:
        return release_obj.get("assets", []) or []
    assets = []
    for page in range(1, 11):
        separator = "&" if "?" in assets_url else "?"
        page_url = f"{assets_url}{separator}per_page=100&page={page}"
        try:
            page_assets = json.loads(http_text(page_url))
        except Exception as exc:
            log(f"Could not read GitHub release assets {page_url}: {exc}")
            return assets or (release_obj.get("assets", []) or [])
        if not page_assets:
            break
        assets.extend(page_assets)
        if len(page_assets) < 100:
            break
    return assets or (release_obj.get("assets", []) or [])


def github_direct_release_apks(repo, release_obj, release, arch, target, subtarget, missing_packages, log):
    release_tag = str(release_obj.get("tag_name") or "").strip()
    tags = [release_tag] if release_tag else []
    for candidate in [f"v{release}", release]:
        if candidate not in tags:
            tags.append(candidate)
    suffixes = []
    if arch and target and subtarget:
        suffixes.extend([
            f"_v{release}_{arch}_{target}_{subtarget}.apk",
            f"_{release}_{arch}_{target}_{subtarget}.apk",
        ])
    if arch:
        suffixes.extend([
            f"_v{release}_{arch}.apk",
            f"_{release}_{arch}.apk",
        ])
    links = []
    for package in missing_packages:
        found = False
        for tag in tags:
            if found:
                break
            for suffix in suffixes:
                filename = f"{package}{suffix}"
                direct = (
                    f"https://github.com/{repo}/releases/download/"
                    f"{urllib.parse.quote(tag, safe='')}/{urllib.parse.quote(filename, safe='')}"
                )
                if http_url_exists(direct, timeout=10):
                    log(f"Found GitHub release APK by direct URL: {filename}")
                    links.append(direct)
                    found = True
                    break
    return links


def github_release_objects(repo, release, log):
    release_tags = []
    for candidate in [f"v{release}", release]:
        if candidate not in release_tags:
            release_tags.append(candidate)
    for tag in release_tags:
        api = f"https://api.github.com/repos/{repo}/releases/tags/{urllib.parse.quote(tag, safe='')}"
        try:
            return [json.loads(http_text(api))]
        except Exception as exc:
            log(f"Could not read GitHub release tag {repo}@{tag}: {exc}")
    api = f"https://api.github.com/repos/{repo}/releases"
    try:
        releases = json.loads(http_text(api))
    except Exception as exc:
        log(f"Could not read GitHub releases {repo}: {exc}")
        return []
    wanted = release.lower().lstrip("v")
    matched = []
    for rel in releases:
        tag = str(rel.get("tag_name", "")).lower().lstrip("v")
        name = str(rel.get("name", "")).lower().lstrip("v")
        if wanted in [tag, name] or tag.startswith(wanted):
            matched.append(rel)
    return matched


def list_apks_from_github_releases(url, src, release, arch, target, subtarget, requested, log):
    repo = src.get("github_repo") or github_repo_from_url(url)
    if not repo:
        return []
    direct_links = []
    if requested:
        direct_links = github_direct_release_apks(repo, {"tag_name": f"v{release}"}, release, arch, target, subtarget, requested, log)
        direct_found = {package_name_from_apk(Path(urllib.parse.urlparse(link).path).name) for link in direct_links}
        if all(package in direct_found for package in requested):
            return direct_links
    releases = github_release_objects(repo, release, log)
    if not releases:
        return direct_links
    assets = []
    matched_releases = []
    for rel in releases:
        matched_releases.append(rel)
        assets.extend(github_release_assets(rel, log))
    regex = src.get("regex", "").strip()
    rx = re.compile(regex) if regex else None
    links = []
    for asset in assets:
        filename = asset.get("name", "")
        url = asset.get("browser_download_url", "")
        if not filename.endswith(".apk") or not url:
            continue
        if rx and not rx.search(filename):
            continue
        if not apk_matches_platform(filename, arch, target, subtarget):
            continue
        if not package_requested(filename, requested):
            continue
        links.append(url)
    links.extend(link for link in direct_links if link not in links)
    if requested and matched_releases:
        found = {package_name_from_apk(Path(urllib.parse.urlparse(link).path).name) for link in links}
        missing = [package for package in requested if package not in found]
        if missing:
            links.extend(github_direct_release_apks(repo, matched_releases[0], release, arch, target, subtarget, missing, log))
    return links


def list_apks_from_index(url, src, release, arch, target, subtarget, requested, log):
    try:
        text = http_text(url)
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            log(f"Could not read package repository {url}: HTTP {exc.code}")
        return []
    except Exception as exc:
        log(f"Could not read package repository {url}: {exc}")
        return []
    regex = src.get("regex", "").strip()
    rx = re.compile(regex) if regex else None
    links = []
    for link in parse_links(text):
        if not link.endswith(".apk"):
            continue
        full = urllib.parse.urljoin(url, link)
        filename = Path(urllib.parse.urlparse(full).path).name
        if rx and not rx.search(filename):
            continue
        if not apk_matches_platform(filename, arch, target, subtarget):
            continue
        if not package_requested(filename, requested):
            continue
        links.append(full)
    return links


def choose_latest_per_package(links):
    selected = {}
    for link in links:
        filename = Path(urllib.parse.urlparse(link).path).name
        package = package_name_from_apk(filename)
        old = selected.get(package)
        if old is None or version_key(filename) > version_key(Path(urllib.parse.urlparse(old).path).name):
            selected[package] = link
    return [selected[k] for k in sorted(selected)]


def discover_package_source(src, release, arch, target="", subtarget="", log=lambda _msg: None):
    url = src.get("url", "").strip()
    if not url:
        return []
    requested = split_packages(src.get("packages") or src.get("package_names") or "")
    if url.endswith(".apk"):
        filename = Path(urllib.parse.urlparse(url).path).name
        if requested and not package_requested(filename, requested):
            return []
        # Прямая ссылка на APK игнорирует проверку маски архитектуры в имени файла
        return [url]
    else:
        links = []
        if src.get("type") == "github_releases" or github_repo_from_url(url):
            links.extend(list_apks_from_github_releases(url, src, release, arch, target, subtarget, requested, log))
        for candidate in source_candidate_urls(src, release, arch):
            found = list_apks_from_index(candidate, src, release, arch, target, subtarget, requested, log)
            if found:
                log(f"Found {len(found)} matching APK(s) in {candidate}")
            links.extend(found)
        if not links:
            log(f"No APK matched source {src.get('name') or url}")
            return []
        return choose_latest_per_package(links)


def resolve_package_source(src, release, arch, target, subtarget, log):
    selected_urls = discover_package_source(src, release, arch, target, subtarget, log)
    downloaded = []
    for selected in selected_urls:
        filename = Path(urllib.parse.urlparse(selected).path).name
        dest = DOWNLOAD_DIR / "apk" / release / arch / filename
        if not dest.exists():
            log(f"Downloading APK: {selected}")
            download_file(selected, dest)
        downloaded.append(dest)
    return downloaded


def download_external_apks(cfg, release, router, log):
    arch = router.get("arch", "").strip()
    target = router.get("target", "").strip()
    subtarget = router.get("subtarget", "").strip()
    result = []
    for src in cfg.get("package_sources", []):
        if not src.get("enabled", True):
            continue
        source_arch = src.get("arch", "").strip()
        if source_arch and arch and source_arch != arch:
            continue
        result.extend(resolve_package_source(src, release, arch, target, subtarget, log))
    return result


def source_required_packages(src):
    return split_packages(src.get("packages") or src.get("package_names") or "")


def source_applies_to_router(src, router):
    if not src.get("enabled", True):
        return False
    source_arch = src.get("arch", "").strip()
    router_arch = router.get("arch", "").strip()
    return not (source_arch and router_arch and source_arch != router_arch)


def external_apk_report(cfg, release, router, log=lambda _msg: None):
    arch = router.get("arch", "").strip()
    target = router.get("target", "").strip()
    subtarget = router.get("subtarget", "").strip()
    sources = []
    missing = []
    for src in cfg.get("package_sources", []):
        if not source_applies_to_router(src, router):
            continue
        requested = source_required_packages(src)
        urls = discover_package_source(src, release, arch, target, subtarget, log)
        found_names = {package_name_from_apk(Path(urllib.parse.urlparse(url).path).name) for url in urls}
        missing_names = [name for name in requested if name not in found_names]
        if requested and missing_names:
            missing.append({
                "source": src.get("name") or src.get("url"),
                "missing": missing_names,
            })
        sources.append({
            "name": src.get("name") or src.get("url"),
            "url": src.get("url"),
            "packages": [
                {
                    "name": package_name_from_apk(Path(urllib.parse.urlparse(url).path).name),
                    "file": Path(urllib.parse.urlparse(url).path).name,
                    "url": url,
                }
                for url in urls
            ],
            "missing": missing_names if requested else [],
        })
    return {"sources": sources, "missing": missing, "ready": not missing}


def firmware_manifest_path(router_name, release):
    return OUTPUT_DIR / router_name / release / "manifest.json"


def firmware_history(router_name, limit=3):
    root = OUTPUT_DIR / router_name
    items = []
    if not root.exists():
        return []
    for manifest_path in root.glob("*/manifest.json"):
        if manifest_path.parent.name == "latest":
            continue
        manifest = read_json(manifest_path, {})
        image_name = manifest.get("sysupgrade")
        if not image_name:
            continue
        image_path = manifest_path.parent / image_name
        if not image_path.exists():
            continue
        release = manifest.get("release") or manifest_path.parent.name
        items.append({
            "router": router_name,
            "release": release,
            "built_at": manifest.get("built_at") or "",
            "name": image_name,
            "sha256": manifest.get("sha256", ""),
            "url": f"/firmware/{router_name}/{release}/{image_name}",
        })
    items.sort(key=lambda item: item.get("built_at") or item.get("release") or "", reverse=True)
    return items[:limit]


def prune_router_firmware(router_name, keep=3):
    root = OUTPUT_DIR / router_name
    if not root.exists():
        return
    keep_releases = {item["release"] for item in firmware_history(router_name, keep)}
    for child in root.iterdir():
        if child.name == "latest":
            continue
        if child.is_dir() and child.name not in keep_releases:
            shutil.rmtree(child)


def prune_jobs_state(keep_success_per_router=3):
    st = state()
    kept = []
    success_count = {}
    for job in st.get("jobs", []):
        status = job.get("status")
        router = job.get("router") or ""
        if status in ["failed", "cancelled"]:
            log_name = Path(str(job.get("log", ""))).name
            if log_name:
                try:
                    (LOG_DIR / log_name).unlink()
                except FileNotFoundError:
                    pass
            continue
        if status == "success":
            count = success_count.get(router, 0)
            if count >= keep_success_per_router:
                continue
            success_count[router] = count + 1
        kept.append(job)

    def mutate(current):
        current["jobs"] = kept[:100]
    update_state(mutate)


def prune_all_router_firmware(keep=3):
    cfg = config()
    for router in cfg.get("routers", []):
        name = router.get("name")
        if name:
            prune_router_firmware(name, keep)


def router_statuses(cfg=None, st=None):
    cfg = cfg or config()
    st = st or state()
    latest = st.get("latest_release")
    statuses = {}
    active_statuses = ["queued", "running", "downloading", "checking", "building", "publishing"]
    for router in cfg.get("routers", []):
        name = router.get("name")
        if not name:
            continue
        history = firmware_history(name, 3)
        jobs = [j for j in st.get("jobs", []) if j.get("router") in [name, "all"]]
        active = next((j for j in jobs if j.get("status") in active_statuses), None)
        waiting = next((j for j in jobs if j.get("status") == "waiting_apks"), None)
        success = next((j for j in jobs if j.get("status") == "success"), None)
        if active:
            statuses[name] = {
                "state": "building",
                "label": "Собирается",
                "tooltip": active.get("last_line") or active.get("status"),
                "job": active.get("id"),
                "firmware": history,
            }
        elif waiting:
            statuses[name] = {
                "state": "missing_apks",
                "label": "Нет APK",
                "tooltip": waiting.get("error") or waiting.get("last_line") or "Нет необходимых APK",
                "job": waiting.get("id"),
                "firmware": history,
            }
        elif latest and firmware_manifest_path(name, latest).exists():
            statuses[name] = {
                "state": "no_new_versions",
                "label": "Нет новых версий",
                "tooltip": f"Последняя версия {latest} уже собрана",
                "firmware": history,
            }
        elif success:
            statuses[name] = {
                "state": "success",
                "label": "Успешно собрано",
                "tooltip": success.get("output") or "Прошивка собрана",
                "firmware": history,
            }
        else:
            statuses[name] = {
                "state": "idle",
                "label": "Не собиралось",
                "tooltip": "Для роутера еще нет успешной сборки",
                "firmware": history,
            }
    return statuses


def record_job(job_id, router, release, status, log_path, extra=None):
    def mutate(st):
        jobs = [j for j in st.get("jobs", []) if j.get("id") != job_id]
        previous = next((j for j in st.get("jobs", []) if j.get("id") == job_id), {})
        job = dict(previous)
        job.update({
            "id": job_id,
            "router": router,
            "release": release,
            "status": status,
            "progress": job_progress(status),
            "log": f"/logs/{log_path.name}",
            "last_line": last_log_line(f"/logs/{log_path.name}"),
            "updated_at": utc_now(),
        })
        if extra:
            job.update(extra)
        jobs.insert(0, job)
        st["jobs"] = jobs[:100]
    update_state(mutate)


def append_job_log(log_path, msg):
    line = f"[{utc_now()}] {msg}\n"
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(line)
    print(line, end="", flush=True)


def sanitize_job_part(value):
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(value or "job")).strip("-") or "job"


def is_job_cancelled(job_id):
    with ACTIVE_JOBS_LOCK:
        return job_id in CANCELLED_JOBS


def register_process(job_id, proc):
    with ACTIVE_JOBS_LOCK:
        ACTIVE_JOBS[job_id] = proc


def unregister_process(job_id):
    with ACTIVE_JOBS_LOCK:
        ACTIVE_JOBS.pop(job_id, None)


def cancel_job(job_id):
    log_path = LOG_DIR / f"{Path(job_id).name}.log"
    previous = next((j for j in state().get("jobs", []) if j.get("id") == job_id), {})
    router = previous.get("router") or ""
    release = previous.get("release") or ""
    with ACTIVE_JOBS_LOCK:
        CANCELLED_JOBS.add(job_id)
        proc = ACTIVE_JOBS.get(job_id)
    append_job_log(log_path, "Cancellation requested")
    if proc and proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except Exception:
            proc.terminate()
    record_job(job_id, router, release, "cancelled", log_path, {"error": "Cancelled by user"})
    prune_jobs_state()
    return {"status": "cancelled", "id": job_id}


def enqueue_manual_build(router_name=None, force=True):
    name = router_name or "all"
    job_id = f"{int(time.time())}-manual-{sanitize_job_part(name)}"
    log_path = LOG_DIR / f"{job_id}.log"
    append_job_log(log_path, "Manual build queued")
    record_job(job_id, name, "pending", "queued", log_path)
    thread = threading.Thread(
        target=lambda: manual_build_worker(job_id, log_path, router_name, force),
        daemon=True,
    )
    thread.start()
    return {"status": "queued", "id": job_id, "log": f"/logs/{log_path.name}"}


def manual_build_worker(job_id, log_path, router_name, force):
    try:
        if is_job_cancelled(job_id):
            record_job(job_id, router_name or "all", "pending", "cancelled", log_path, {"error": "Cancelled by user"})
            return
        append_job_log(log_path, "Waiting for build slot")
        with BUILD_LOCK:
            if is_job_cancelled(job_id):
                record_job(job_id, router_name or "all", "pending", "cancelled", log_path, {"error": "Cancelled by user"})
                return
            cfg = config()
            release = latest_openwrt_release(cfg.get("release_branch_prefix", "25."), allow_stale_cache=False)
            run_build(release, router_name=router_name, force=force, job_id_override=job_id, log_path_override=log_path)
    except Exception as exc:
        append_job_log(log_path, f"ERROR: {exc}")
        status = "cancelled" if is_job_cancelled(job_id) else "failed"
        record_job(job_id, router_name or "all", "pending", status, log_path, {"error": str(exc)})
    finally:
        unregister_process(job_id)


def waiting_job_for_missing_apks(release, router, report):
    name = router.get("name", "router")
    job_id = f"wait-apks-{release}-{re.sub(r'[^a-zA-Z0-9_.-]+', '-', name)}"
    log_path = LOG_DIR / f"{job_id}.log"
    missing_text = "; ".join(
        f"{item['source']}: {', '.join(item['missing'])}" for item in report.get("missing", [])
    )
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(f"[{utc_now()}] Waiting for APKs for OpenWrt {release}: {missing_text}\n")
    record_job(job_id, name, release, "waiting_apks", log_path, {
        "error": f"External APKs are not available yet: {missing_text}",
    })


def split_packages(text):
    if isinstance(text, list):
        return [str(x).strip() for x in text if str(x).strip()]
    return [p for p in re.split(r"[\s,\n]+", str(text or "")) if p.strip()]


def copy_apks_to_builder(builder_dir, apks):
    local_dir = builder_dir / "local-apks"
    local_dir.mkdir(exist_ok=True)
    copied = []
    for apk in apks:
        dest = local_dir / apk.name
        shutil.copy2(apk, dest)
        copied.append(dest)
    return copied


def patch_imagebuilder_for_untrusted_apk(builder_dir, log):
    patched = []
    candidates = [builder_dir / "Makefile"]
    include_dir = builder_dir / "include"
    if include_dir.exists():
        candidates.extend(include_dir.rglob("*.mk"))
    patterns = [
        (re.compile(r"(\$\(APK\)\s+add\s+)"), r"\1--allow-untrusted "),
        (re.compile(r"(\bapk\s+add\s+)"), r"\1--allow-untrusted "),
        (re.compile(r"(\S+/apk\s+add\s+)"), r"\1--allow-untrusted "),
    ]
    for path in candidates:
        if not path.exists() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        new_lines = []
        for line in text.splitlines(keepends=True):
            if "--allow-untrusted" in line:
                new_lines.append(line)
                continue
            for rx, repl in patterns:
                line = rx.sub(repl, line)
            new_lines.append(line)
        new_text = "".join(new_lines)
        if new_text != text:
            path.write_text(new_text, encoding="utf-8")
            patched.append(path.relative_to(builder_dir).as_posix())
    if patched:
        log("Patched ImageBuilder apk calls for untrusted APK support: " + ", ".join(patched))
    else:
        log("No ImageBuilder apk add calls were patched; passing allow-untrusted make flags as fallback")


def extract_apk_overlay(apks, files_dir, log):
    files_dir.mkdir(parents=True, exist_ok=True)
    root = files_dir.resolve()
    for apk in apks:
        if apk.suffix == ".apk":
            log(f"Skipping APK overlay extraction; ImageBuilder will install package: {apk.name}")
            continue
        log(f"Extracting untrusted APK overlay: {apk.name}")
        try:
            with tarfile.open(apk, "r:*") as tar:
                members = []
                for m in tar.getmembers():
                    name = m.name.lstrip("./")
                    if not name or name.startswith(".PKGINFO") or name.startswith(".SIGN.") or name.startswith(".CONTROL"):
                        continue
                    target = (root / name).resolve()
                    if not str(target).startswith(str(root) + os.sep):
                        log(f"Skipping unsafe APK member path: {m.name}")
                        continue
                    if name.startswith("etc/") or name.startswith("usr/") or name.startswith("lib/") or name.startswith("sbin/") or name.startswith("bin/"):
                        members.append(m)
                tar.extractall(files_dir, members=members)
        except Exception as exc:
            log(f"Could not extract {apk.name}; ImageBuilder will still receive the APK path: {exc}")


def find_sysupgrade_image(out_dir, profile):
    candidates = list(out_dir.rglob("*sysupgrade*.bin")) + list(out_dir.rglob("*sysupgrade*.itb")) + list(out_dir.rglob("*sysupgrade*.tar"))
    if profile:
        filtered = [p for p in candidates if profile.replace(",", "_") in p.name or profile in p.name]
        if filtered:
            candidates = filtered
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def run_build(release, router_name=None, force=False, job_id_override=None, log_path_override=None):
    cfg = config()
    routers = [r for r in cfg.get("routers", []) if r.get("enabled", True)]
    if router_name:
        routers = [r for r in routers if r.get("name") == router_name]
    if not routers:
        raise RuntimeError("No enabled routers configured")

    for router in routers:
        name = router["name"]
        job_id = job_id_override or f"{int(time.time())}-{sanitize_job_part(name)}"
        log_path = log_path_override or LOG_DIR / f"{job_id}.log"

        def log(msg):
            line = f"[{utc_now()}] {msg}\n"
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(line)
            print(line, end="", flush=True)

        def set_job(status, extra=None):
            record_job(job_id, name, release, status, log_path, extra)

        if is_job_cancelled(job_id):
            log("Build cancelled before start")
            set_job("cancelled", {"error": "Cancelled by user"})
            continue
        set_job("running")
        try:
            profile = router["profile"]
            built_marker = firmware_manifest_path(name, release)
            if built_marker.exists() and not force:
                log("Firmware already exists; skipping")
                set_job("skipped", {"output": f"/firmware/{name}/{release}/"})
                continue
            set_job("downloading")
            builder = ensure_imagebuilder(release, router, log)
            set_job("checking")
            apks = download_external_apks(cfg, release, router, log)
            local_apks = copy_apks_to_builder(builder, apks)
            if cfg.get("allow_untrusted_apk", True) and local_apks:
                patch_imagebuilder_for_untrusted_apk(builder, log)
            files_dir = builder / "files"
            if cfg.get("allow_untrusted_apk", True):
                extract_apk_overlay(local_apks, files_dir, log)

            packages = split_packages(router.get("packages", ""))
            package_args = packages + [str(p) for p in local_apks]
            cmd = ["make", "image", f"PROFILE={profile}", f"PACKAGES={' '.join(package_args)}"]
            if files_dir.exists():
                cmd.append(f"FILES={files_dir}")
            env = os.environ.copy()
            if cfg.get("allow_untrusted_apk", True):
                env["APK_FLAGS"] = "--allow-untrusted"
                env["APK_ADD_FLAGS"] = "--allow-untrusted"
                env["APK_OPTS"] = "--allow-untrusted"
                env["OPENWRT_BUILDER_ALLOW_UNTRUSTED_APK"] = "1"
                if local_apks:
                    cmd.extend([
                        "APK_FLAGS=--allow-untrusted",
                        "APK_ADD_FLAGS=--allow-untrusted",
                        "APK_OPTS=--allow-untrusted",
                    ])
            set_job("building")
            log("Running: " + " ".join(cmd))
            with log_path.open("a", encoding="utf-8") as logfh:
                proc = subprocess.Popen(cmd, cwd=builder, env=env, stdout=logfh, stderr=subprocess.STDOUT, start_new_session=True)
                register_process(job_id, proc)
                return_code = proc.wait()
                unregister_process(job_id)
            if is_job_cancelled(job_id):
                log("Build cancelled")
                set_job("cancelled", {"error": "Cancelled by user"})
                continue
            if return_code != 0:
                raise RuntimeError(f"ImageBuilder failed with exit code {return_code}")
            image = find_sysupgrade_image(builder / "bin", profile)
            if not image:
                raise RuntimeError("No sysupgrade image was produced")
            set_job("publishing")
            dest_dir = OUTPUT_DIR / name / release
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest_image = dest_dir / image.name
            shutil.copy2(image, dest_image)
            manifest = {
                "router": name,
                "release": release,
                "target": router["target"],
                "subtarget": router["subtarget"],
                "profile": profile,
                "arch": router.get("arch", ""),
                "packages": packages,
                "external_apks": [p.name for p in local_apks],
                "sysupgrade": dest_image.name,
                "sha256": sha256_file(dest_image),
                "built_at": utc_now(),
            }
            write_json(dest_dir / "manifest.json", manifest)
            latest_dir = OUTPUT_DIR / name / "latest"
            if latest_dir.exists() or latest_dir.is_symlink():
                if latest_dir.is_symlink() or latest_dir.is_file():
                    latest_dir.unlink()
                else:
                    shutil.rmtree(latest_dir)
            shutil.copytree(dest_dir, latest_dir)
            log(f"Firmware ready: /firmware/{name}/{release}/{dest_image.name}")
            log(f"Build completed successfully: {dest_image.name}")
            set_job("success", {"output": f"/firmware/{name}/{release}/{dest_image.name}"})
            prune_router_firmware(name, keep=3)
            prune_jobs_state()
        except Exception as exc:
            log(f"ERROR: {exc}")
            set_job("failed", {"error": str(exc)})
            prune_jobs_state()


def check_and_build(force=False, router_name=None):
    cfg = config()
    release = latest_openwrt_release(cfg.get("release_branch_prefix", "25."), allow_stale_cache=False)

    def mutate(st):
        st["last_check"] = utc_now()
        st["latest_release"] = release
    update_state(mutate)

    routers = [r for r in cfg.get("routers", []) if r.get("enabled", True)]
    if router_name:
        routers = [r for r in routers if r.get("name") == router_name]
    built_any = False
    for router in routers:
        name = router.get("name")
        if not name:
            continue
        if not force and firmware_manifest_path(name, release).exists():
            continue
        report = external_apk_report(cfg, release, router)
        if not report.get("ready", True):
            waiting_job_for_missing_apks(release, router, report)
            continue
        run_build(release, router_name=name, force=force)
        built_any = True

    if built_any:
        def mark(st):
            st["built_release"] = release
        update_state(mark)
    return release


def asu_overview():
    st = state()
    cfg = config()
    try:
        latest = latest_openwrt_release(cfg.get("release_branch_prefix", "25."), timeout=10, allow_stale_cache=True)
    except Exception:
        latest = st.get("latest_release")
    branch = cfg.get("release_branch_prefix", "25.").rstrip(".")
    targets = sorted({
        f"{r.get('target')}/{r.get('subtarget')}"
        for r in cfg.get("routers", [])
        if r.get("enabled", True) and r.get("target") and r.get("subtarget")
    })
    latest_versions = [latest] if latest else []
    branch_keys = []
    if branch:
        branch_keys.append(branch)
    for version in latest_versions:
        version_branch = ".".join(str(version).replace("-SNAPSHOT", "").split(".")[:2])
        if version_branch and version_branch not in branch_keys:
            branch_keys.append(version_branch)
    branch_info = {
        "name": "",
        "versions": latest_versions,
        "targets": targets,
        "path": "releases/{version}",
        "pubkey": "",
        "snapshot": False,
        "package_changes": [],
    }
    return {
        "latest": latest_versions,
        "branches": {
            key: {**branch_info, "name": key}
            for key in branch_keys
        },
        "upstream_url": "https://downloads.openwrt.org",
        "server": {
            "version": "local-openwrt-builder-1.0",
            "contact": "local",
            "allow_defaults": False,
            "repository_allow_list": [],
            "max_custom_rootfs_size_mb": 1024,
            "max_defaults_length": 0,
        },
        "versions": latest_versions,
        "profiles": [
            {
                "name": r.get("name"),
                "target": r.get("target"),
                "subtarget": r.get("subtarget"),
                "profile": r.get("profile"),
                "arch": r.get("arch"),
                "enabled": r.get("enabled", True),
            }
            for r in cfg.get("routers", [])
        ],
        "note": "This is a local prebuilder. Use /firmware/<router>/latest/ for ready sysupgrade images.",
    }


def scan_repositories():
    cfg = config()
    release = latest_openwrt_release(cfg.get("release_branch_prefix", "25."))
    report = []

    def quiet_log(_msg):
        return None

    for router in [r for r in cfg.get("routers", []) if r.get("enabled", True)]:
        arch = router.get("arch", "").strip()
        package_report = external_apk_report(cfg, release, router, quiet_log)
        router_report = {
            "router": router.get("name"),
            "arch": arch,
            "release": release,
            "ready": package_report.get("ready", True),
            "missing": package_report.get("missing", []),
            "sources": package_report.get("sources", []),
        }
        report.append(router_report)
    return {"release": release, "routers": report, "checked_at": utc_now()}


def clear_old_jobs():
    for root in [LOG_DIR, DOWNLOAD_DIR, BUILD_DIR]:
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True, exist_ok=True)

    def mutate(st):
        st["jobs"] = []
        st["last_error"] = ""
        st["cleanup_at"] = utc_now()
    update_state(mutate)
    return {"status": "ok", "cleaned_at": utc_now()}


def firmware_history_response(router_name):
    return {"router": router_name, "firmware": firmware_history(router_name, 3)}


def asu_latest():
    overview = asu_overview()
    return {"latest": overview.get("latest", [])}


def asu_branches():
    return list(asu_overview().get("branches", {}).values())


def asu_revision(version, target, subtarget):
    try:
        profiles = load_profiles_json(version, f"{target}/{subtarget}")
        return {"revision": profiles.get("version_code", "")}
    except Exception as exc:
        return {"detail": f"Failed to fetch revision for {version}/{target}/{subtarget}: {exc}", "status": 400}


def asu_job_response(job_id):
    job = next((j for j in state().get("jobs", []) if j.get("id") == job_id), None)
    if not job:
        return {"status": 404, "detail": "could not find provided request hash"}, 404
    status = job.get("status")
    if status in ["queued", "running", "downloading", "checking", "building", "publishing"]:
        return {
            "status": 202,
            "detail": "started",
            "request_hash": job_id,
            "imagebuilder_status": {
                "queued": "init",
                "running": "init",
                "downloading": "download_imagebuilder",
                "checking": "validate_manifest",
                "building": "building_image",
                "publishing": "signing_images",
            }.get(status, "init"),
        }, 202
    if status == "success" and job.get("output"):
        output = job["output"].lstrip("/")
        parts = output.split("/")
        router = parts[1] if len(parts) > 1 else job.get("router", "")
        release = parts[2] if len(parts) > 2 else job.get("release", "")
        image_name = parts[-1]
        manifest = read_json(OUTPUT_DIR / router / release / "manifest.json", {})
        sha = manifest.get("sha256", "")
        return {
            "status": 200,
            "request_hash": job_id,
            "version_number": release,
            "version_code": "",
            "target": f"{manifest.get('target', '')}/{manifest.get('subtarget', '')}".strip("/"),
            "id": manifest.get("profile") or router,
            "build_at": manifest.get("built_at") or job.get("updated_at"),
            "bin_dir": f"{router}/{release}",
            "images": [{
                "name": image_name,
                "type": "sysupgrade",
                "filesystem": "squashfs",
                "sha256": sha,
                "sha256_unsigned": sha,
            }],
        }, 200
    return {
        "status": 500,
        "detail": job.get("error") or status or "failed",
        "request_hash": job_id,
        "stderr": last_log_line(job.get("log", "")),
    }, 500


class Scheduler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.wakeup = threading.Event()

    def run(self):
        while True:
            cfg = config()
            try:
                with BUILD_LOCK:
                    check_and_build(force=False)
            except Exception as exc:
                def mutate(st):
                    st["last_error"] = str(exc)
                    st["last_check"] = utc_now()
                update_state(mutate)
            interval = max(5, int(cfg.get("check_interval_minutes", 360))) * 60
            self.wakeup.wait(interval)
            self.wakeup.clear()


scheduler = Scheduler()


INDEX_HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>OpenWrt Custom Local Builder</title>
  <style>
    :root {
      --bg: #f8fafd;
      --surface: #ffffff;
      --surface-2: #f1f5fb;
      --text: #1f1f1f;
      --muted: #5f6368;
      --border: #dfe3eb;
      --primary: #1a73e8;
      --primary-hover: #1558b0;
      --primary-soft: #e8f0fe;
      --danger: #b3261e;
      --danger-soft: #fce8e6;
      --success: #137333;
      --shadow: 0 1px 2px rgba(60,64,67,.12), 0 8px 24px rgba(60,64,67,.08);
    }
    * { box-sizing: border-box; }
    body { margin: 0; font-family: Inter, Roboto, "Segoe UI", Arial, sans-serif; background: var(--bg); color: var(--text); font-size: 14px; }
    header { position: sticky; top: 0; z-index: 10; background: rgba(255,255,255,.92); backdrop-filter: blur(14px); border-bottom: 1px solid var(--border); }
    .topbar { max-width: 1240px; margin: 0 auto; min-height: 72px; padding: 0 24px; display: flex; align-items: center; justify-content: space-between; gap: 18px; }
    .brand { display: flex; align-items: center; gap: 14px; min-width: 0; }
    .brand-mark { width: 40px; height: 40px; border-radius: 8px; background: linear-gradient(135deg, #1a73e8, #34a853); color: white; display: grid; place-items: center; font-weight: 800; letter-spacing: 0; box-shadow: 0 8px 18px rgba(26,115,232,.22); text-decoration: none; }
    .brand-mark:hover { text-decoration: none; }
    h1 { font-size: 19px; line-height: 1.2; margin: 0; font-weight: 700; }
    .brand-subtitle { margin-top: 3px; color: var(--muted); font-size: 12px; }
    main { max-width: 1240px; margin: 0 auto; padding: 24px; display: grid; gap: 18px; }
    section { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 18px; box-shadow: 0 1px 2px rgba(60,64,67,.06); }
    .section-head { display: flex; align-items: center; justify-content: space-between; gap: 14px; margin-bottom: 14px; }
    h2 { font-size: 16px; line-height: 1.3; margin: 0; font-weight: 700; }
    label { display: grid; gap: 7px; font-size: 12px; color: var(--muted); font-weight: 650; }
    input, textarea, select { width: 100%; border: 1px solid #c7d0dd; border-radius: 8px; padding: 11px 12px; font: inherit; color: var(--text); background: #fff; outline: none; transition: border-color .15s ease, box-shadow .15s ease, background .15s ease; }
    input:focus, textarea:focus, select:focus { border-color: var(--primary); box-shadow: 0 0 0 3px rgba(26,115,232,.14); }
    textarea { min-height: 92px; resize: vertical; }
    button { border: 0; border-radius: 8px; padding: 10px 15px; font: inherit; font-size: 13px; font-weight: 700; background: var(--primary); color: white; cursor: pointer; transition: background .15s ease, box-shadow .15s ease, transform .08s ease; }
    button:hover { background: var(--primary-hover); box-shadow: 0 3px 10px rgba(26,115,232,.18); }
    button:active { transform: translateY(1px); }
    button.secondary { background: var(--primary-soft); color: #174ea6; }
    button.secondary:hover { background: #d2e3fc; box-shadow: none; }
    button.danger { background: var(--danger-soft); color: var(--danger); }
    button.danger:hover { background: #fad2cf; box-shadow: none; }
    .grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; }
    .row { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
    .toolbar { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; justify-content: space-between; }
    .item { border: 1px solid var(--border); border-radius: 8px; padding: 14px; display: grid; gap: 10px; margin-bottom: 10px; background: #fff; }
    .table-wrap { overflow-x: auto; border: 1px solid var(--border); border-radius: 8px; }
    table { width: 100%; border-collapse: collapse; min-width: 760px; background: #fff; }
    th, td { text-align: left; border-bottom: 1px solid #edf1f7; padding: 13px 14px; vertical-align: middle; }
    tr:last-child td { border-bottom: 0; }
    th { font-size: 11px; color: var(--muted); font-weight: 800; text-transform: uppercase; letter-spacing: .04em; background: #f8fafd; }
    tbody tr:hover { background: #f8fbff; }
    .actions { display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
    .modal-backdrop { position: fixed; inset: 0; background: rgba(32,33,36,.46); display: none; align-items: center; justify-content: center; padding: 18px; z-index: 20; }
    .modal-backdrop.open { display: flex; }
    .modal { width: min(980px, 100%); max-height: 92vh; overflow: auto; background: white; border-radius: 8px; border: 1px solid var(--border); box-shadow: var(--shadow); padding: 20px; display: grid; gap: 16px; }
    .modal.log-modal { width: min(1080px, 100%); }
    .modal-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding-bottom: 4px; }
    .modal-head h2 { margin: 0; font-size: 19px; }
    textarea.packages { min-height: 190px; font-family: "Roboto Mono", ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 13px; line-height: 1.5; }
    .muted { color: var(--muted); font-size: 13px; }
    .pill { display: inline-flex; align-items: center; background: var(--primary-soft); color: #174ea6; border-radius: 999px; padding: 4px 9px; font-size: 12px; font-weight: 700; }
    .status-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 6px; background: var(--success); }
    .job-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
    .progress { height: 8px; border-radius: 999px; background: #edf1f7; overflow: hidden; }
    .progress > span { display: block; height: 100%; width: 0; background: var(--primary); transition: width .2s ease; }
    .last-line { font-family: "Roboto Mono", ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 12px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    code { background: var(--surface-2); border: 1px solid var(--border); border-radius: 6px; padding: 2px 6px; }
    pre { white-space: pre-wrap; background: #202124; color: #e8eaed; padding: 14px; border-radius: 8px; max-height: 64vh; overflow: auto; }
    footer { max-width: 1240px; margin: 0 auto; padding: 0 24px 28px; color: var(--muted); }
    .footer-bar { border: 1px solid var(--border); background: var(--surface); border-radius: 8px; padding: 14px 16px; display: flex; align-items: center; justify-content: space-between; gap: 12px; box-shadow: 0 1px 2px rgba(60,64,67,.06); }
    .footer-meta { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
    a { color: var(--primary); text-decoration: none; font-weight: 700; }
    a:hover { text-decoration: underline; }
    @media (max-width: 860px) { .grid { grid-template-columns: 1fr; } .topbar, main { padding-left: 16px; padding-right: 16px; } .topbar { display: grid; height: auto; padding-top: 14px; padding-bottom: 14px; } .toolbar, .section-head { align-items: stretch; } }
  </style>
</head>
<body>
<header>
  <div class="topbar">
    <div class="brand">
      <a class="brand-mark" href="/" title="Home" data-i18n-title="home">OW</a>
      <div>
        <h1>OpenWrt Custom Local Builder</h1>
        <div class="brand-subtitle" data-i18n="subtitle">Локальная сборка прошивок для OpenWrt 25.x</div>
      </div>
    </div>
    <div class="row">
      <span id="latest" class="pill"><span class="status-dot"></span>...</span>
      <select id="languageSelect" onchange="setLanguage(this.value)" style="width:auto; min-width:82px">
        <option value="ru">RU</option>
        <option value="en">EN</option>
      </select>
      <button onclick="buildAllAvailable()" data-i18n="buildAllAvailable">Собрать все доступные прошивки</button>
    </div>
  </div>
</header>
<main>
  <section>
    <div class="section-head">
      <h2 data-i18n="mainSettings">Основные настройки</h2>
      <div class="row">
        <span id="settingsSaveStatus" class="muted" data-i18n="autosaveEnabled">Автосохранение включено</span>
        <button onclick="save()" data-i18n="saveSettings">Сохранить настройки</button>
      </div>
    </div>
    <div class="grid">
      <label><span data-i18n="publicUrl">Публичный URL сервера</span> <input id="public_base_url" oninput="scheduleSave()"></label>
      <label><span data-i18n="releaseBranch">Ветка релизов</span> <input id="release_branch_prefix" placeholder="25." oninput="scheduleSave()"></label>
      <label><span data-i18n="checkEveryMinutes">Проверять каждые, минут</span> <input id="check_interval_minutes" type="number" min="5" oninput="scheduleSave()"></label>
      <label><span data-i18n="allowUntrusted">Разрешить untrusted APK</span> <select id="allow_untrusted_apk" onchange="scheduleSave()"><option value="true" data-i18n="yes">Да</option><option value="false" data-i18n="no">Нет</option></select></label>
    </div>
  </section>
  <section>
    <div class="section-head">
      <h2 data-i18n="routers">Роутеры</h2>
      <button class="secondary" onclick="openRouterModal()" data-i18n="addRouter">Добавить роутер</button>
    </div>
    <div id="routers"></div>
  </section>
  <section>
    <div class="section-head">
      <h2 data-i18n="sources">Внешние APK / репозитории</h2>
      <button class="secondary" onclick="openSourceModal()" data-i18n="addSource">Добавить источник</button>
    </div>
    <div id="sources"></div>
  </section>
  <section>
    <div class="toolbar">
      <div class="row">
        <button onclick="save()" data-i18n="save">Сохранить</button>
        <button class="secondary" onclick="load()" data-i18n="refreshStatus">Обновить статус</button>
        <button class="secondary" onclick="scanRepos()" data-i18n="checkRepos">Проверить репозитории</button>
      </div>
      <p class="muted">Sysupgrade server: <code id="sysurl"></code></p>
    </div>
  </section>
  <section>
      <h2 data-i18n="repoCheck">Проверка репозиториев</h2>
    <div id="scan" class="muted" data-i18n="notRunYet">Еще не запускалась.</div>
  </section>
  <section>
    <div class="section-head">
      <h2 data-i18n="jobs">Задания</h2>
      <button class="danger" onclick="cleanupJobs()" data-i18n="cleanupJobs">Очистить старые задания</button>
    </div>
    <div id="jobs"></div>
  </section>
</main>
<footer>
  <div class="footer-bar">
    <div class="footer-meta">
      <a id="repoLink" href="https://github.com/nick2ld/openwrt-builder" target="_blank" rel="noreferrer">GitHub</a>
      <span id="versionStatus" data-i18n="checkingVersion">Проверка версии...</span>
    </div>
    <div class="row">
      <button class="secondary" onclick="checkVersion()" data-i18n="checkVersion">Проверить версию</button>
      <button onclick="runUpdate()" data-i18n="update">Обновить</button>
    </div>
  </div>
</footer>
<div id="routerModal" class="modal-backdrop">
  <div class="modal">
    <div class="modal-head">
      <h2 id="routerModalTitle">Роутер</h2>
      <button class="secondary" onclick="closeRouterModal()" data-i18n="close">Закрыть</button>
    </div>
    <label><span data-i18n="deviceSearchLabel">Поиск модели как в Firmware Selector</span> <input id="modalDeviceSearch" placeholder="Например: Cudy WR3000, GL-MT6000, Archer C7" data-i18n-placeholder="deviceSearchPlaceholder" oninput="searchDevices()"></label>
    <div id="modalDeviceResults"></div>
    <div class="grid">
      <label><span data-i18n="name">Название</span> <input id="routerName"></label>
      <label>Target <input id="routerTarget" placeholder="mediatek"></label>
      <label>Subtarget <input id="routerSubtarget" placeholder="filogic"></label>
      <label>Profile <input id="routerProfile" placeholder="cudy_wr3000-v1"></label>
    </div>
    <div class="grid">
      <label>Arch APK <input id="routerArch" placeholder="aarch64_cortex-a53"></label>
      <label><span data-i18n="enabled">Включен</span> <select id="routerEnabled"><option value="true" data-i18n="yes">Да</option><option value="false" data-i18n="no">Нет</option></select></label>
    </div>
    <label><span data-i18n="routerPackages">Пакеты прошивки для этого роутера</span> <textarea id="routerPackages" class="packages" spellcheck="false"></textarea></label>
    <div class="row">
      <button onclick="saveRouterModal()" data-i18n="saveRouter">Сохранить роутер</button>
      <button class="secondary" onclick="closeRouterModal()" data-i18n="cancel">Отмена</button>
    </div>
  </div>
</div>
<div id="sourceModal" class="modal-backdrop">
  <div class="modal">
    <div class="modal-head">
      <h2 id="sourceModalTitle">Источник APK</h2>
      <button class="secondary" onclick="closeSourceModal()" data-i18n="close">Закрыть</button>
    </div>
    <div class="grid">
      <label><span data-i18n="name">Название</span> <input id="sourceName" placeholder="custom-packages"></label>
      <label><span data-i18n="sourceUrl">URL .apk, repo или GitHub releases</span> <input id="sourceUrl" placeholder="https://github.com/Slava-Shchipunov/awg-openwrt/releases"></label>
      <label><span data-i18n="archFilter">Arch фильтр</span> <input id="sourceArch" placeholder="aarch64_cortex-a53"></label>
      <label><span data-i18n="enabled">Включен</span> <select id="sourceEnabled"><option value="true" data-i18n="yes">Да</option><option value="false" data-i18n="no">Нет</option></select></label>
    </div>
    <label><span data-i18n="sourcePackages">Имена пакетов из этого repo</span> <textarea id="sourcePackages" placeholder="my-package another-package"></textarea></label>
    <label><span data-i18n="sourceRegex">Regex имени APK</span> <input id="sourceRegex" placeholder="my-package_.*\\.apk"></label>
    <div class="row">
      <button onclick="saveSourceModal()" data-i18n="saveSource">Сохранить источник</button>
      <button class="secondary" onclick="closeSourceModal()" data-i18n="cancel">Отмена</button>
    </div>
  </div>
</div>
<div id="firmwareModal" class="modal-backdrop">
  <div class="modal">
    <div class="modal-head">
      <h2 id="firmwareModalTitle">Последние прошивки</h2>
      <button class="secondary" onclick="closeFirmwareModal()" data-i18n="close">Закрыть</button>
    </div>
    <div id="firmwareLinks"></div>
  </div>
</div>
<div id="logModal" class="modal-backdrop">
  <div class="modal log-modal">
    <div class="modal-head">
      <h2 id="logTitle">Лог задания</h2>
      <div class="row">
        <button class="secondary" onclick="refreshSelectedLog({forceUpdate:true})" data-i18n="refresh">Обновить</button>
        <button class="secondary" onclick="copySelectedLog()" data-i18n="copy">Копировать</button>
        <button class="secondary" onclick="closeLogModal()" data-i18n="close">Закрыть</button>
      </div>
    </div>
    <pre id="mainLog"></pre>
  </div>
</div>
<script>
let cfg = {};
let searchTimer = null;
let saveTimer = null;
let dirty = false;
let deviceResultCache = [];
let editingRouterIndex = null;
let editingSourceIndex = null;
let selectedLogUrl = '';
let selectedLogLabel = '';
let jobCache = [];
let routerStatusCache = {};
let messages = {};
let currentLang = localStorage.getItem('owb_language') || ((navigator.language || '').toLowerCase().startsWith('ru') ? 'ru' : 'en');
let logAutoScroll = true;

function t(key, params = {}) {
  let text = messages[key] || key;
  for (const [name, value] of Object.entries(params)) text = text.replaceAll(`{${name}}`, value);
  return text;
}

async function loadLocale(lang) {
  currentLang = lang;
  languageSelect.value = lang;
  localStorage.setItem('owb_language', lang);
  document.documentElement.lang = lang;
  try {
    const res = await fetch(`/locales/${encodeURIComponent(lang)}.json`, {cache: 'no-store'});
    if (!res.ok) throw new Error(res.statusText);
    messages = await res.json();
  } catch (e) {
    messages = {};
  }
  applyI18n();
  renderRouters();
  renderSources();
  if (jobCache.length) jobs.innerHTML = jobCache.map(renderJob).join('');
}

function setLanguage(lang) {
  loadLocale(lang);
}

function applyI18n() {
  document.querySelectorAll('[data-i18n]').forEach(el => {
    el.textContent = t(el.getAttribute('data-i18n'));
  });
  document.querySelectorAll('[data-i18n-placeholder]').forEach(el => {
    el.setAttribute('placeholder', t(el.getAttribute('data-i18n-placeholder')));
  });
  document.querySelectorAll('[data-i18n-title]').forEach(el => {
    el.setAttribute('title', t(el.getAttribute('data-i18n-title')));
  });
}

function routerStatusLabel(status, fallback) {
  const map = {
    no_new_versions: 'routerStatusNoNew',
    missing_apks: 'routerStatusMissingApks',
    building: 'routerStatusBuilding',
    success: 'routerStatusSuccess',
    queued: 'routerStatusQueued',
    idle: 'routerStatusIdle',
    unknown: 'routerStatusUnknown'
  };
  return map[status] ? t(map[status]) : (fallback || t('routerStatusUnknown'));
}

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

function renderRouters() {
  const list = cfg.routers || [];
  if (!list.length) {
    routers.innerHTML = `<div class="item"><b>${esc(t('noRoutersTitle'))}</b><div class="muted">${esc(t('noRoutersText'))}</div></div>`;
    return;
  }
  routers.innerHTML = `
    <div class="table-wrap">
    <table>
      <thead><tr><th>${esc(t('name'))}</th><th>${esc(t('status'))}</th><th>${esc(t('profile'))}</th><th>${esc(t('target'))}</th><th>${esc(t('arch'))}</th><th></th></tr></thead>
      <tbody>
        ${list.map((r, i) => `
          ${(() => { const st = routerStatusCache[r.name] || {}; return `
          <tr>
            <td><b>${esc(r.name || t('router'))}</b><div class="muted">${r.enabled === false ? esc(t('disabledValue')) : esc(t('enabledValue'))}</div></td>
            <td><span class="pill" title="${esc(st.tooltip || '')}">${esc(routerStatusLabel(st.state, st.label))}</span></td>
            <td>${esc(r.profile || '')}</td>
            <td>${esc(r.target || '')}/${esc(r.subtarget || '')}</td>
            <td>${esc(r.arch || '')}</td>
            <td><div class="actions">
              <button class="secondary" onclick="openRouterModal(${i})">${esc(t('edit'))}</button>
              <button class="secondary" onclick="buildRouterByIndex(${i})">${esc(t('build'))}</button>
              <button class="secondary" onclick="openFirmwareModal(${i})">${esc(t('firmwares'))}</button>
              <button class="danger" onclick="deleteRouter(${i})">${esc(t('delete'))}</button>
            </div></td>
          </tr>`})()}`).join('')}
      </tbody>
    </table>
    </div>`;
}

function renderSources() {
  const list = cfg.package_sources || [];
  if (!list.length) {
    sources.innerHTML = `<div class="item"><b>${esc(t('noSourcesTitle'))}</b><div class="muted">${esc(t('noSourcesText'))}</div></div>`;
    return;
  }
  sources.innerHTML = `
    <div class="table-wrap">
      <table>
        <thead><tr><th>${esc(t('name'))}</th><th>URL</th><th>${esc(t('arch'))}</th><th>${esc(t('packages'))}</th><th></th></tr></thead>
        <tbody>
          ${list.map((s, i) => `
            <tr>
              <td><b>${esc(s.name || 'source')}</b><div class="muted">${s.enabled === false ? esc(t('disabledValue')) : esc(t('enabledValue'))}</div></td>
              <td>${esc(s.url || '')}</td>
              <td>${esc(s.arch || t('anyArch'))}</td>
              <td>${esc(s.packages || s.package_names || s.regex || t('regexOrAll'))}</td>
              <td><div class="actions">
                <button class="secondary" onclick="openSourceModal(${i})">${esc(t('edit'))}</button>
                <button class="danger" onclick="deleteSource(${i})">${esc(t('delete'))}</button>
              </div></td>
            </tr>`).join('')}
        </tbody>
      </table>
    </div>`;
}

function bindConfig() {
  for (const id of ['public_base_url','release_branch_prefix','check_interval_minutes']) document.getElementById(id).value = cfg[id] ?? '';
  allow_untrusted_apk.value = cfg.allow_untrusted_apk ? 'true' : 'false';
  sysurl.textContent = (cfg.public_base_url || location.origin);
  renderRouters();
  renderSources();
}

function pullForm() {
  cfg.public_base_url = public_base_url.value;
  cfg.release_branch_prefix = release_branch_prefix.value || '25.';
  cfg.check_interval_minutes = Number(check_interval_minutes.value || 360);
  cfg.allow_untrusted_apk = allow_untrusted_apk.value === 'true';
}

function scheduleSave() {
  dirty = true;
  settingsSaveStatus.textContent = t('unsavedChanges');
  clearTimeout(saveTimer);
  saveTimer = setTimeout(save, 800);
}

function uniqueRouterName(base) {
  base = base || 'router';
  const existing = new Set((cfg.routers || []).map(r => r.name));
  let name = base;
  let index = 2;
  while (existing.has(name)) name = `${base}-${index++}`;
  return name;
}

function emptyRouter() {
  return {name: uniqueRouterName('router'), target:'', subtarget:'', profile:'', arch:'', packages:'luci luci-app-attendedsysupgrade', enabled:true};
}

function openRouterModal(index = null) {
  editingRouterIndex = index;
  const router = index === null ? emptyRouter() : {...(cfg.routers[index] || emptyRouter())};
  routerModalTitle.textContent = index === null ? t('addRouter') : t('editRouter');
  routerName.value = router.name || '';
  routerTarget.value = router.target || '';
  routerSubtarget.value = router.subtarget || '';
  routerProfile.value = router.profile || '';
  routerArch.value = router.arch || '';
  routerEnabled.value = router.enabled === false ? 'false' : 'true';
  routerPackages.value = router.packages || '';
  modalDeviceSearch.value = '';
  modalDeviceResults.innerHTML = '';
  deviceResultCache = [];
  routerModal.classList.add('open');
}

function closeRouterModal() {
  routerModal.classList.remove('open');
  editingRouterIndex = null;
}

function routerFromModal() {
  return {
    name: routerName.value.trim() || uniqueRouterName('router'),
    target: routerTarget.value.trim(),
    subtarget: routerSubtarget.value.trim(),
    profile: routerProfile.value.trim(),
    arch: routerArch.value.trim(),
    packages: routerPackages.value.trim(),
    enabled: routerEnabled.value === 'true'
  };
}

async function saveRouterModal() {
  cfg.routers = cfg.routers || [];
  const router = routerFromModal();
  if (editingRouterIndex === null) {
    router.name = uniqueRouterName(router.name);
    cfg.routers.push(router);
  } else {
    cfg.routers[editingRouterIndex] = router;
  }
  renderRouters();
  closeRouterModal();
  await save();
}

function fillModalFromDevice(device) {
  const safeName = device.name || device.profile || t('router');
  const baseName = safeName.toLowerCase().replace(/[^a-z0-9_.-]+/g, '-').replace(/^-|-$/g, '') || device.profile;
  if (!routerName.value || editingRouterIndex === null) routerName.value = uniqueRouterName(baseName);
  routerTarget.value = device.target || '';
  routerSubtarget.value = device.subtarget || '';
  routerProfile.value = device.profile || '';
  routerArch.value = device.arch || '';
  routerPackages.value = device.packages || routerPackages.value || 'luci luci-app-attendedsysupgrade';
  modalDeviceResults.innerHTML = '';
  modalDeviceSearch.value = '';
}

function addDeviceRouterByIndex(index) {
  const device = deviceResultCache[index];
  if (device) fillModalFromDevice(device);
}

function buildRouterByIndex(index) {
  const router = (cfg.routers || [])[index];
  if (router) buildNow(router.name);
}

function deleteRouter(index) {
  cfg.routers.splice(index, 1);
  renderRouters();
  save();
}

async function openFirmwareModal(index) {
  const router = (cfg.routers || [])[index];
  if (!router) return;
  firmwareModalTitle.textContent = `${t('firmwareModalTitle')}: ${router.name}`;
  firmwareLinks.innerHTML = `<div class="muted">${esc(t('loading'))}</div>`;
  firmwareModal.classList.add('open');
  try {
    const report = await api('/api/router-firmware/' + encodeURIComponent(router.name));
    const list = report.firmware || [];
    firmwareLinks.innerHTML = list.length ? list.map(item => `
      <div class="item">
        <div><b>${esc(item.release)}</b><div class="muted">${esc(item.built_at || '')}</div></div>
        <div><a href="${esc(item.url)}">${esc(item.name)}</a></div>
        <div class="muted">${esc(item.sha256 || '')}</div>
      </div>`).join('') : `<div class="item"><b>${esc(t('noFirmwareTitle'))}</b><div class="muted">${esc(t('noFirmwareText'))}</div></div>`;
  } catch (e) {
    firmwareLinks.innerHTML = `<div class="item"><b>${esc(t('firmwareLoadError'))}</b><div class="muted">${esc(e.message)}</div></div>`;
  }
}

function closeFirmwareModal() {
  firmwareModal.classList.remove('open');
}

function renderDeviceResults(report) {
  if (report.error) {
    modalDeviceResults.innerHTML = `<div class="item"><b>${esc(t('deviceLoadError'))}</b><div class="muted">${esc(report.error)}</div></div>`;
    return;
  }
  const devices = report.devices || [];
  deviceResultCache = devices;
  modalDeviceResults.innerHTML = devices.length ? devices.map((d, i) => `
    <div class="item">
      <div><b>${esc(d.name)}</b> <span class="pill">${esc(d.profile)}</span></div>
      <div class="muted">${esc(d.target)}/${esc(d.subtarget)} · ${esc(d.arch || 'arch unknown')} · OpenWrt ${esc(report.release)}</div>
      <button class="secondary" onclick="addDeviceRouterByIndex(${i})">${esc(t('fieldsLoaded'))}</button>
    </div>`).join('') : `<div class="muted">${esc(t('nothingFound'))}</div>`;
}

function searchDevices() {
  clearTimeout(searchTimer);
  const q = modalDeviceSearch.value.trim();
  if (q.length < 2) {
    modalDeviceResults.innerHTML = '';
    return;
  }
  searchTimer = setTimeout(async () => {
    modalDeviceResults.innerHTML = `<div class="muted">${esc(t('searching'))}</div>`;
    try {
      const report = await api('/api/devices?q=' + encodeURIComponent(q));
      renderDeviceResults(report);
    } catch (e) {
      modalDeviceResults.innerHTML = '<div class="muted">' + esc(e.message) + '</div>';
    }
  }, 300);
}

function emptySource() {
  return {name:'custom', url:'', arch:'', packages:'', regex:'', enabled:true};
}

function openSourceModal(index = null) {
  editingSourceIndex = index;
  const source = index === null ? emptySource() : {...(cfg.package_sources[index] || emptySource())};
  sourceModalTitle.textContent = index === null ? t('addSource') : t('editSource');
  sourceName.value = source.name || '';
  sourceUrl.value = source.url || '';
  sourceArch.value = source.arch || '';
  sourceEnabled.value = source.enabled === false ? 'false' : 'true';
  sourcePackages.value = source.packages || source.package_names || '';
  sourceRegex.value = source.regex || '';
  sourceModal.classList.add('open');
}

function closeSourceModal() {
  sourceModal.classList.remove('open');
  editingSourceIndex = null;
}

function sourceFromModal() {
  return {
    name: sourceName.value.trim() || 'custom',
    url: sourceUrl.value.trim(),
    arch: sourceArch.value.trim(),
    packages: sourcePackages.value.trim(),
    regex: sourceRegex.value.trim(),
    enabled: sourceEnabled.value === 'true'
  };
}

async function saveSourceModal() {
  cfg.package_sources = cfg.package_sources || [];
  const source = sourceFromModal();
  if (editingSourceIndex === null) {
    cfg.package_sources.push(source);
  } else {
    cfg.package_sources[editingSourceIndex] = source;
  }
  renderSources();
  closeSourceModal();
  await save();
}

function deleteSource(index) {
  cfg.package_sources.splice(index, 1);
  renderSources();
  save();
}

async function save() {
  clearTimeout(saveTimer);
  pullForm();
  settingsSaveStatus.textContent = t('saving');
  try {
    await api('/api/config', {method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify(cfg)});
    dirty = false;
    settingsSaveStatus.textContent = t('saved');
    await load();
  } catch (e) {
    settingsSaveStatus.textContent = t('saveError') + e.message;
    throw e;
  }
}

async function buildNow(router) {
  pullForm();
  await api('/api/config', {method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify(cfg)});
  const result = await api('/api/build', {method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify({router, force:true})});
  await load();
  if (result.log) viewLog(result.log, t('buildStarted'));
}

async function buildAllAvailable() {
  pullForm();
  await api('/api/config', {method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify(cfg)});
  const result = await api('/api/build', {method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify({force:false})});
  await load();
  if (result.log) viewLog(result.log, t('buildStarted'));
}

async function scanRepos() {
  pullForm();
  scan.innerHTML = esc(t('checking'));
  await api('/api/config', {method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify(cfg)});
  const report = await api('/api/scan-packages', {method:'POST'});
  scan.innerHTML = report.routers.map(r => `
    <div class="item">
      <b>${esc(r.router)} ${esc(r.release)}</b>
      <span class="pill">${r.ready ? esc(t('apkFound')) : esc(t('waitingApk'))}</span>
      <span class="muted">${esc(r.arch)}</span>
      ${(r.missing || []).length ? `<div class="muted">${esc(t('missing'))}: ${(r.missing || []).map(m => esc(m.source) + ': ' + (m.missing || []).map(esc).join(', ')).join('; ')}</div>` : ''}
      ${r.sources.map(s => `
        <div>
          <b>${esc(s.name)}</b>
          ${s.packages.length ? s.packages.map(p => `<div><a href="${esc(p.url)}">${esc(p.file)}</a></div>`).join('') : `<div class="muted">${esc(t('noMatchingApk'))}</div>`}
        </div>`).join('')}
    </div>`).join('');
}

async function viewLog(url, label = '') {
  selectedLogUrl = url;
  selectedLogLabel = label || url;
  logTitle.textContent = selectedLogLabel;
  logModal.classList.add('open');
  logAutoScroll = true;
  await refreshSelectedLog({forceScroll: true});
}

function viewJobLog(index) {
  const job = jobCache[index];
  if (job && job.log) viewLog(job.log, `${job.router} ${job.release}`);
}

function closeLogModal() {
  logModal.classList.remove('open');
}

function isLogScrolledToBottom() {
  return mainLog.scrollHeight - mainLog.scrollTop - mainLog.clientHeight < 24;
}

function logSelectionText() {
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed || selection.rangeCount === 0) return '';
  for (let i = 0; i < selection.rangeCount; i++) {
    const range = selection.getRangeAt(i);
    if (range.intersectsNode(mainLog)) return selection.toString();
  }
  return '';
}

async function refreshSelectedLog(options = {}) {
  if (!selectedLogUrl) return;
  try {
    const selectingLog = Boolean(logSelectionText());
    if (!options.forceUpdate && !options.forceScroll && (selectingLog || !logAutoScroll)) return;
    const shouldScroll = options.forceScroll || (logAutoScroll && isLogScrolledToBottom() && !selectingLog);
    const res = await fetch(selectedLogUrl, {cache: 'no-store'});
    mainLog.textContent = await res.text();
    if (shouldScroll) mainLog.scrollTop = mainLog.scrollHeight;
  } catch (e) {
    mainLog.textContent = e.message;
  }
}

function selectedLogText() {
  const selected = logSelectionText();
  if (selected) return selected;
  return mainLog.textContent || '';
}

async function writeClipboardText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.setAttribute('readonly', '');
  textarea.style.position = 'fixed';
  textarea.style.left = '-9999px';
  document.body.appendChild(textarea);
  textarea.select();
  const ok = document.execCommand('copy');
  textarea.remove();
  if (!ok) throw new Error('copy command failed');
}

async function copySelectedLog() {
  const text = selectedLogText();
  if (!text) return;
  const old = logTitle.textContent;
  try {
    await writeClipboardText(text);
    logTitle.textContent = t('logCopied');
  } catch (e) {
    logTitle.textContent = t('copyFailed') + ': ' + e.message;
  }
  setTimeout(() => logTitle.textContent = old, 1200);
}

async function checkVersion() {
  versionStatus.textContent = t('checking');
  const info = await api('/api/version');
  repoLink.href = info.repo_url;
  const current = info.current_short || 'unknown';
  const latest = info.latest_short || 'unknown';
  if (!info.current_short) {
    versionStatus.textContent = t('versionNoCommit', {latest});
  } else if (info.update_available) {
    versionStatus.textContent = info.latest_cached
      ? t('updateAvailableCached', {current, latest})
      : t('updateAvailable', {current, latest});
  } else if (info.error) {
    versionStatus.textContent = info.latest_cached
      ? t('versionCached', {current})
      : t('versionGithubError', {current});
  } else {
    versionStatus.textContent = t('currentVersion', {current});
  }
}

async function runUpdate() {
  versionStatus.textContent = t('startingUpdate');
  try {
    const result = await api('/api/update', {method:'POST'});
    versionStatus.textContent = t('updateStarted');
    if (result.log) viewLog(result.log, t('updateLogTitle'));
  } catch (e) {
    if (String(e.message || '').includes('Failed to fetch')) {
      versionStatus.textContent = t('updateConnectionLost');
    } else {
      versionStatus.textContent = t('updateFailed') + e.message;
    }
    viewLog('/logs/self-update.log', t('updateLogTitle'));
  }
}

async function cleanupJobs() {
  if (!confirm(t('cleanupConfirm'))) return;
  await api('/api/cleanup', {method:'POST'});
  selectedLogUrl = '';
  selectedLogLabel = '';
  mainLog.textContent = '';
  closeLogModal();
  await load();
}

function renderJob(job, index) {
  const progress = Math.max(0, Math.min(100, Number(job.progress ?? 0)));
  const output = job.output ? `<a href="${esc(job.output)}">${esc(t('firmwareLink'))}</a>` : '';
  const error = job.error ? `<div class="muted">${esc(job.error)}</div>` : '';
  const canCancel = ['queued','running','downloading','checking','building','publishing','waiting_apks'].includes(job.status);
  const cancel = canCancel ? `<button class="danger" onclick="cancelJob(${index})">${esc(t('stop'))}</button>` : '';
  const finalLine = job.status === 'success' && job.output ? `${t('ready')}: ${job.output}` : (job.last_line || t('emptyLog'));
  return `<div class="item">
    <div class="job-head">
      <div><b>${esc(job.router)} ${esc(job.release)}</b><div class="muted">${esc(job.updated_at || '')}</div></div>
      <span class="pill">${esc(job.status)}</span>
    </div>
    <div class="progress"><span style="width:${progress}%"></span></div>
    <div class="last-line">${esc(finalLine)}</div>
    ${error}
    <div class="row">${output}<button class="secondary" onclick="viewJobLog(${index})">${esc(t('log'))}</button>${cancel}</div>
  </div>`;
}

async function cancelJob(index) {
  const job = jobCache[index];
  if (!job || !job.id) return;
  if (!confirm(t('stopConfirm', {job: job.router || job.id}))) return;
  await api('/api/cancel', {method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify({id: job.id})});
  await load();
}

async function load() {
  if (!dirty) cfg = await api('/api/config');
  const st = await api('/api/status');
  latest.textContent = t('latest') + ': ' + (st.latest_release || 'unknown');
  const jobList = st.jobs || [];
  jobCache = jobList;
  routerStatusCache = st.routers_status || {};
  jobs.innerHTML = jobList.length ? jobList.map(renderJob).join('') : `<div class="item"><b>${esc(t('noJobsTitle'))}</b><div class="muted">${esc(t('noJobsText'))}</div></div>`;
  if (selectedLogUrl && logModal.classList.contains('open')) {
    refreshSelectedLog();
  }
  if (!dirty) bindConfig();
}
loadLocale(currentLang).then(() => {
  load();
  checkVersion();
});
setInterval(load, 3000);
mainLog.addEventListener('scroll', () => {
  logAutoScroll = isLogScrolledToBottom();
});
mainLog.addEventListener('mousedown', () => {
  logAutoScroll = false;
});
mainLog.addEventListener('keydown', () => {
  logAutoScroll = false;
});
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[{utc_now()}] {self.address_string()} {fmt % args}", flush=True)

    def cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Requested-With")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Cache-Control", "no-store")

    def send(self, status, body, content_type="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.cors_headers()
        self.end_headers()

    def read_body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/":
            self.send(200, INDEX_HTML, "text/html; charset=utf-8")
        elif path.startswith("/locales/"):
            name = Path(urllib.parse.unquote(path[len("/locales/"):])).name
            file_path = LOCALE_DIR / name
            if file_path.suffix == ".json" and file_path.exists():
                self.send(200, file_path.read_text(encoding="utf-8"), "application/json; charset=utf-8")
            else:
                self.send(404, {"error": "not found"})
        elif path == "/api/config":
            self.send(200, config())
        elif path == "/api/status":
            self.send(200, enriched_state())
        elif path.startswith("/api/router-firmware/"):
            router = urllib.parse.unquote(path[len("/api/router-firmware/"):])
            self.send(200, firmware_history_response(router))
        elif path == "/api/version":
            self.send(200, version_status())
        elif path == "/api/devices":
            params = urllib.parse.parse_qs(parsed.query)
            query = params.get("q", [""])[0]
            try:
                limit = int(params.get("limit", ["20"])[0])
            except ValueError:
                limit = 20
            try:
                self.send(200, search_devices(query, limit=max(1, min(limit, 50))))
            except Exception as exc:
                self.send(200, {"release": None, "devices": [], "error": str(exc)})
        elif path in [
            "/api/overview",
            "/api/v1/overview",
            "/json/v1/overview.json",
            "/api/asu/json/v1/overview.json",
            "/api/asu/api/v1/overview",
        ]:
            self.send(200, asu_overview())
        elif path in ["/json/v1/latest.json", "/api/asu/json/v1/latest.json", "/api/v1/latest"]:
            self.send(200, asu_latest())
        elif path in ["/json/v1/branches.json", "/api/asu/json/v1/branches.json"]:
            self.send(200, asu_branches())
        elif path.startswith("/api/v1/revision/") or path.startswith("/api/asu/api/v1/revision/"):
            prefix = "/api/asu/api/v1/revision/" if path.startswith("/api/asu/") else "/api/v1/revision/"
            parts = path[len(prefix):].split("/")
            if len(parts) >= 3:
                self.send(200, asu_revision(parts[0], parts[1], "/".join(parts[2:])))
            else:
                self.send(400, {"error": "bad revision path"})
        elif path.startswith("/api/v1/build/") or path.startswith("/api/asu/api/v1/build/"):
            prefix = "/api/asu/api/v1/build/" if path.startswith("/api/asu/") else "/api/v1/build/"
            body, status = asu_job_response(urllib.parse.unquote(path[len(prefix):]))
            self.send(status, body)
        elif path.startswith("/logs/"):
            file_path = LOG_DIR / Path(path).name
            if file_path.exists():
                self.send(200, read_log_response(file_path), "text/plain; charset=utf-8")
            else:
                self.send(404, {"error": "not found"})
        elif path.startswith("/firmware/") or path.startswith("/store/"):
            prefix = "/store/" if path.startswith("/store/") else "/firmware/"
            file_path = OUTPUT_DIR / Path(urllib.parse.unquote(path[len(prefix):]))
            if file_path.is_dir():
                listing = []
                for p in sorted(file_path.iterdir()):
                    listing.append({"name": p.name, "url": path.rstrip("/") + "/" + urllib.parse.quote(p.name)})
                self.send(200, listing)
            elif file_path.exists():
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(file_path.stat().st_size))
                self.cors_headers()
                self.end_headers()
                with file_path.open("rb") as fh:
                    shutil.copyfileobj(fh, self.wfile)
            else:
                self.send(404, {"error": "not found"})
        elif path == "/api/asu":
            self.send(200, {
                "service": "local-openwrt-builder",
                "note": "Set the Attended Sysupgrade server URL to the root server URL, for example http://IP:8088. Compatibility paths under /api/asu are kept for older saved settings.",
                "firmware": "/firmware/",
            })
        else:
            self.send(404, {"error": "not found"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/config":
            body = self.read_body()
            merged = DEFAULT_CONFIG.copy()
            merged.update(body)
            write_json(CONFIG_PATH, merged)
            scheduler.wakeup.set()
            self.send(200, merged)
        elif path == "/api/build":
            body = self.read_body()
            router = body.get("router")
            force = bool(body.get("force", True))
            self.send(202, enqueue_manual_build(router_name=router, force=force))
        elif path == "/api/scan-packages":
            self.send(200, scan_repositories())
        elif path == "/api/update":
            try:
                self.send(202, run_self_update())
            except Exception as exc:
                self.send(500, {"status": "failed", "error": str(exc), "log": "/logs/self-update.log"})
        elif path == "/api/cleanup":
            self.send(200, clear_old_jobs())
        elif path == "/api/cancel":
            body = self.read_body()
            job_id = str(body.get("id", "")).strip()
            if not job_id:
                self.send(400, {"error": "missing job id"})
            else:
                self.send(200, cancel_job(job_id))
        elif path in ["/api/v1/build", "/api/asu/api/v1/build", "/api/build-request"]:
            body = self.read_body()
            board = body.get("profile") or body.get("board") or body.get("target")
            router = None
            for item in config().get("routers", []):
                if board in [item.get("name"), item.get("profile")]:
                    router = item.get("name")
                    break
            queued = enqueue_manual_build(router_name=router, force=True)
            self.send(202, {
                "status": "queued",
                "request_hash": queued.get("id"),
                "imagebuilder_status": "init",
                "queue_position": 0,
                "detail": "Local build queued. Poll the web UI or /firmware/<router>/latest/ for the finished sysupgrade image.",
            })
        else:
            self.send(404, {"error": "not found"})


def main():
    ensure_dirs()
    prune_all_router_firmware(keep=3)
    prune_jobs_state()
    cfg = config()
    scheduler.start()
    server = ThreadingHTTPServer((cfg.get("listen_host", "0.0.0.0"), int(cfg.get("listen_port", 8088))), Handler)
    print(f"OpenWrt Custom Local Builder listening on http://{cfg.get('listen_host')}:{cfg.get('listen_port')}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
