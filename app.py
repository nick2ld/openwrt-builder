#!/usr/bin/env python3
import hashlib
import html
import json
import os
import re
import shutil
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
DOWNLOAD_DIR = DATA_DIR / "downloads"
BUILD_DIR = DATA_DIR / "builders"
OUTPUT_DIR = DATA_DIR / "firmware"
LOG_DIR = DATA_DIR / "logs"
CONFIG_PATH = DATA_DIR / "config.json"
STATE_PATH = DATA_DIR / "state.json"
DEVICE_CACHE = {}
RELEASE_CACHE_TTL = 3600
HTTP_TIMEOUT = int(os.environ.get("OWB_HTTP_TIMEOUT", "20"))

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


def config():
    cfg = DEFAULT_CONFIG.copy()
    cfg.update(read_json(CONFIG_PATH, DEFAULT_CONFIG))
    return cfg


def state():
    return read_json(STATE_PATH, {"last_check": None, "latest_release": None, "jobs": []})


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
    for sep in ["_", "-"]:
        if sep in name:
            return name.split(sep, 1)[0]
    return name[:-4]


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


def apk_matches_arch(filename, arch):
    if not arch:
        return True
    return filename.endswith(f"_{arch}.apk") or filename.endswith("_all.apk") or arch in filename


def list_apks_from_index(url, src, release, arch, requested, log):
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
        if not apk_matches_arch(filename, arch):
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


def discover_package_source(src, release, arch, log):
    url = src.get("url", "").strip()
    if not url:
        return []
    requested = split_packages(src.get("packages") or src.get("package_names") or "")
    if url.endswith(".apk"):
        filename = Path(urllib.parse.urlparse(url).path).name
        if requested and not package_requested(filename, requested):
            return []
        if not apk_matches_arch(filename, arch):
            return []
        return [url]
    else:
        links = []
        for candidate in source_candidate_urls(src, release, arch):
            found = list_apks_from_index(candidate, src, release, arch, requested, log)
            if found:
                log(f"Found {len(found)} matching APK(s) in {candidate}")
            links.extend(found)
        if not links:
            log(f"No APK matched source {src.get('name') or url}")
            return []
        return choose_latest_per_package(links)


def resolve_package_source(src, release, arch, log):
    selected_urls = discover_package_source(src, release, arch, log)
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
    result = []
    for src in cfg.get("package_sources", []):
        if not src.get("enabled", True):
            continue
        source_arch = src.get("arch", "").strip()
        if source_arch and arch and source_arch != arch:
            continue
        result.extend(resolve_package_source(src, release, arch, log))
    return result


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


def extract_apk_overlay(apks, files_dir, log):
    files_dir.mkdir(parents=True, exist_ok=True)
    root = files_dir.resolve()
    for apk in apks:
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


def run_build(release, router_name=None, force=False):
    cfg = config()
    routers = [r for r in cfg.get("routers", []) if r.get("enabled", True)]
    if router_name:
        routers = [r for r in routers if r.get("name") == router_name]
    if not routers:
        raise RuntimeError("No enabled routers configured")

    for router in routers:
        name = router["name"]
        job_id = f"{int(time.time())}-{re.sub(r'[^a-zA-Z0-9_.-]+', '-', name)}"
        log_path = LOG_DIR / f"{job_id}.log"

        def log(msg):
            line = f"[{utc_now()}] {msg}\n"
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(line)
            print(line, end="", flush=True)

        def set_job(status, extra=None):
            def mutate(st):
                jobs = [j for j in st.get("jobs", []) if j.get("id") != job_id]
                job = {
                    "id": job_id,
                    "router": name,
                    "release": release,
                    "status": status,
                    "log": f"/logs/{log_path.name}",
                    "updated_at": utc_now(),
                }
                if extra:
                    job.update(extra)
                jobs.insert(0, job)
                st["jobs"] = jobs[:50]
            update_state(mutate)

        set_job("running")
        try:
            profile = router["profile"]
            built_marker = OUTPUT_DIR / name / release / "manifest.json"
            if built_marker.exists() and not force:
                log("Firmware already exists; skipping")
                set_job("skipped", {"output": f"/firmware/{name}/{release}/"})
                continue
            builder = ensure_imagebuilder(release, router, log)
            apks = download_external_apks(cfg, release, router, log)
            local_apks = copy_apks_to_builder(builder, apks)
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
                env["OPENWRT_BUILDER_ALLOW_UNTRUSTED_APK"] = "1"
            log("Running: " + " ".join(cmd))
            with log_path.open("a", encoding="utf-8") as logfh:
                proc = subprocess.run(cmd, cwd=builder, env=env, stdout=logfh, stderr=subprocess.STDOUT)
            if proc.returncode != 0:
                raise RuntimeError(f"ImageBuilder failed with exit code {proc.returncode}")
            image = find_sysupgrade_image(builder / "bin", profile)
            if not image:
                raise RuntimeError("No sysupgrade image was produced")
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
            set_job("success", {"output": f"/firmware/{name}/{release}/{dest_image.name}"})
        except Exception as exc:
            log(f"ERROR: {exc}")
            set_job("failed", {"error": str(exc)})


def check_and_build(force=False, router_name=None):
    cfg = config()
    release = latest_openwrt_release(cfg.get("release_branch_prefix", "25."), allow_stale_cache=False)

    def mutate(st):
        st["last_check"] = utc_now()
        st["latest_release"] = release
    update_state(mutate)

    current = state().get("built_release")
    if force or current != release or router_name:
        run_build(release, router_name=router_name, force=force)

        def mark(st):
            st["built_release"] = release
        update_state(mark)
    return release


def asu_overview():
    st = state()
    cfg = config()
    latest = st.get("latest_release")
    return {
        "server": {
            "version": "local-openwrt-builder-1.0",
            "contact": "local",
            "allow_defaults": False,
            "repository_allow_list": [],
            "max_custom_rootfs_size_mb": 1024,
            "max_defaults_length": 0,
        },
        "versions": [latest] if latest else [],
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
        router_report = {
            "router": router.get("name"),
            "arch": arch,
            "release": release,
            "sources": [],
        }
        for src in cfg.get("package_sources", []):
            if not src.get("enabled", True):
                continue
            source_arch = src.get("arch", "").strip()
            if source_arch and arch and source_arch != arch:
                continue
            urls = discover_package_source(src, release, arch, quiet_log)
            router_report["sources"].append({
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
            })
        report.append(router_report)
    return {"release": release, "routers": report, "checked_at": utc_now()}


class Scheduler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.wakeup = threading.Event()

    def run(self):
        while True:
            cfg = config()
            try:
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
  <title>OpenWrt Builder</title>
  <style>
    body { margin: 0; font-family: system-ui, -apple-system, Segoe UI, sans-serif; background: #f5f7fb; color: #172033; }
    header { background: #152033; color: white; padding: 18px 24px; display: flex; gap: 16px; justify-content: space-between; align-items: center; }
    main { max-width: 1180px; margin: 0 auto; padding: 24px; display: grid; gap: 18px; }
    section { background: white; border: 1px solid #dfe5ef; border-radius: 8px; padding: 18px; }
    h1 { font-size: 20px; margin: 0; }
    h2 { font-size: 16px; margin: 0 0 12px; }
    label { display: grid; gap: 6px; font-size: 13px; color: #45546d; }
    input, textarea, select { width: 100%; box-sizing: border-box; border: 1px solid #c9d3e3; border-radius: 6px; padding: 9px 10px; font: inherit; background: white; }
    textarea { min-height: 82px; resize: vertical; }
    button { border: 0; border-radius: 6px; padding: 9px 13px; font-weight: 650; background: #1d6fd6; color: white; cursor: pointer; }
    button.secondary { background: #e8edf5; color: #172033; }
    button.danger { background: #bc2f43; }
    .grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
    .row { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
    .item { border: 1px solid #dfe5ef; border-radius: 8px; padding: 12px; display: grid; gap: 10px; margin-bottom: 10px; }
    details.item { display: block; }
    summary { cursor: pointer; list-style: none; display: flex; justify-content: space-between; gap: 12px; align-items: center; }
    summary::-webkit-details-marker { display: none; }
    .details-body { display: grid; gap: 10px; margin-top: 12px; }
    textarea.packages { min-height: 160px; font-family: ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 13px; line-height: 1.45; }
    .muted { color: #64748b; font-size: 13px; }
    .pill { display: inline-flex; background: #eef4ff; color: #245aa3; border-radius: 999px; padding: 4px 8px; font-size: 12px; }
    pre { white-space: pre-wrap; background: #0f172a; color: #dbeafe; padding: 12px; border-radius: 8px; max-height: 260px; overflow: auto; }
    @media (max-width: 860px) { .grid { grid-template-columns: 1fr; } header { display: grid; } }
  </style>
</head>
<body>
<header>
  <h1>OpenWrt 25.x Local ImageBuilder</h1>
  <div class="row">
    <span id="latest" class="pill">...</span>
    <button onclick="buildNow()">Собрать сейчас</button>
  </div>
</header>
<main>
  <section>
    <h2>Основные настройки</h2>
    <div class="grid">
      <label>Публичный URL сервера <input id="public_base_url" oninput="scheduleSave()"></label>
      <label>Ветка релизов <input id="release_branch_prefix" placeholder="25." oninput="scheduleSave()"></label>
      <label>Проверять каждые, минут <input id="check_interval_minutes" type="number" min="5" oninput="scheduleSave()"></label>
      <label>Разрешить untrusted APK <select id="allow_untrusted_apk" onchange="scheduleSave()"><option value="true">Да</option><option value="false">Нет</option></select></label>
    </div>
  </section>
  <section>
    <div class="row" style="justify-content:space-between">
      <h2>Роутеры</h2>
      <button class="secondary" onclick="addRouter()">Добавить роутер</button>
    </div>
    <label>Поиск модели как в Firmware Selector <input id="deviceSearch" placeholder="Например: GL-MT6000, Archer C7, OpenWrt One" oninput="searchDevices()"></label>
    <div id="deviceResults"></div>
    <div id="routers"></div>
  </section>
  <section>
    <div class="row" style="justify-content:space-between">
      <h2>Внешние APK / репозитории</h2>
      <button class="secondary" onclick="addSource()">Добавить источник</button>
    </div>
    <div id="sources"></div>
  </section>
  <section>
    <div class="row">
      <button onclick="save()">Сохранить</button>
      <button class="secondary" onclick="load()">Обновить статус</button>
      <button class="secondary" onclick="scanRepos()">Проверить репозитории</button>
    </div>
    <p class="muted">Для LuCI Attended Sysupgrade укажите сервер: <code id="sysurl"></code></p>
  </section>
  <section>
    <h2>Проверка репозиториев</h2>
    <div id="scan" class="muted">Еще не запускалась.</div>
  </section>
  <section>
    <h2>Задания</h2>
    <div id="jobs"></div>
  </section>
</main>
<script>
let cfg = {};
let searchTimer = null;
let saveTimer = null;
let dirty = false;
let deviceResultCache = [];

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

function renderRouters() {
  routers.innerHTML = (cfg.routers || []).map((r, i) => `
    <details class="item" open>
      <summary>
        <span><b>${esc(r.name || 'router')}</b> <span class="pill">${esc(r.profile || 'profile')}</span></span>
        <span class="muted">${esc(r.target || '?')}/${esc(r.subtarget || '?')} · ${esc(r.arch || 'arch?')}</span>
      </summary>
      <div class="details-body">
      <div class="grid">
        <label>Название <input value="${esc(r.name)}" oninput="cfg.routers[${i}].name=this.value; scheduleSave()"></label>
        <label>Target <input value="${esc(r.target)}" placeholder="mediatek" oninput="cfg.routers[${i}].target=this.value; scheduleSave()"></label>
        <label>Subtarget <input value="${esc(r.subtarget)}" placeholder="filogic" oninput="cfg.routers[${i}].subtarget=this.value; scheduleSave()"></label>
        <label>Profile <input value="${esc(r.profile)}" placeholder="glinet_gl-mt6000" oninput="cfg.routers[${i}].profile=this.value; scheduleSave()"></label>
      </div>
      <div class="grid">
        <label>Arch APK <input value="${esc(r.arch)}" placeholder="aarch64_cortex-a53" oninput="cfg.routers[${i}].arch=this.value; scheduleSave()"></label>
        <label>Включен <select onchange="cfg.routers[${i}].enabled=this.value==='true'; scheduleSave()"><option value="true" ${r.enabled!==false?'selected':''}>Да</option><option value="false" ${r.enabled===false?'selected':''}>Нет</option></select></label>
      </div>
      <label>Пакеты прошивки для этого роутера <textarea class="packages" spellcheck="false" oninput="cfg.routers[${i}].packages=this.value; scheduleSave()">${esc(r.packages)}</textarea></label>
      <div class="row"><button class="danger" onclick="cfg.routers.splice(${i},1);renderRouters(); save()">Удалить</button><button class="secondary" onclick="buildNow('${esc(r.name)}')">Собрать этот</button></div>
      </div>
    </details>`).join('');
}

function renderSources() {
  sources.innerHTML = (cfg.package_sources || []).map((s, i) => `
    <div class="item">
      <div class="grid">
        <label>Название <input value="${esc(s.name)}" oninput="cfg.package_sources[${i}].name=this.value; scheduleSave()"></label>
        <label>URL .apk или repo <input value="${esc(s.url)}" placeholder="https://repo.local/{release}/{arch}/" oninput="cfg.package_sources[${i}].url=this.value; scheduleSave()"></label>
        <label>Arch фильтр <input value="${esc(s.arch)}" placeholder="aarch64_cortex-a53" oninput="cfg.package_sources[${i}].arch=this.value; scheduleSave()"></label>
        <label>Включен <select onchange="cfg.package_sources[${i}].enabled=this.value==='true'; scheduleSave()"><option value="true" ${s.enabled!==false?'selected':''}>Да</option><option value="false" ${s.enabled===false?'selected':''}>Нет</option></select></label>
      </div>
      <label>Имена пакетов из этого repo <textarea placeholder="my-package another-package" oninput="cfg.package_sources[${i}].packages=this.value; scheduleSave()">${esc(s.packages || s.package_names || '')}</textarea></label>
      <label>Regex имени APK <input value="${esc(s.regex)}" placeholder="my-package_.*\\.apk" oninput="cfg.package_sources[${i}].regex=this.value; scheduleSave()"></label>
      <button class="danger" onclick="cfg.package_sources.splice(${i},1);renderSources(); save()">Удалить</button>
    </div>`).join('');
}

function bindConfig() {
  for (const id of ['public_base_url','release_branch_prefix','check_interval_minutes']) document.getElementById(id).value = cfg[id] ?? '';
  allow_untrusted_apk.value = cfg.allow_untrusted_apk ? 'true' : 'false';
  sysurl.textContent = (cfg.public_base_url || location.origin) + '/api/asu';
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
  clearTimeout(saveTimer);
  saveTimer = setTimeout(save, 800);
}

function addRouter() {
  cfg.routers = cfg.routers || [];
  cfg.routers.push({name:uniqueRouterName('router'), target:'', subtarget:'', profile:'', arch:'', packages:'luci luci-app-attendedsysupgrade owut', enabled:true});
  renderRouters();
  save();
}

function uniqueRouterName(base) {
  base = base || 'router';
  const existing = new Set((cfg.routers || []).map(r => r.name));
  let name = base;
  let index = 2;
  while (existing.has(name)) name = `${base}-${index++}`;
  return name;
}

function addDeviceRouter(device) {
  cfg.routers = cfg.routers || [];
  const safeName = device.name || device.profile || 'router';
  const baseName = safeName.toLowerCase().replace(/[^a-z0-9_.-]+/g, '-').replace(/^-|-$/g, '') || device.profile;
  cfg.routers.push({
    name: uniqueRouterName(baseName),
    target: device.target || '',
    subtarget: device.subtarget || '',
    profile: device.profile || '',
    arch: device.arch || '',
    packages: device.packages || 'luci luci-app-attendedsysupgrade',
    enabled: true
  });
  deviceResults.innerHTML = '';
  deviceSearch.value = '';
  renderRouters();
  save();
}

function addDeviceRouterByIndex(index) {
  const device = deviceResultCache[index];
  if (device) addDeviceRouter(device);
}

function renderDeviceResults(report) {
  if (report.error) {
    deviceResults.innerHTML = '<div class="item"><b>Не удалось загрузить список устройств</b><div class="muted">' + esc(report.error) + '</div></div>';
    return;
  }
  const devices = report.devices || [];
  deviceResultCache = devices;
  deviceResults.innerHTML = devices.length ? devices.map((d, i) => `
    <div class="item">
      <div><b>${esc(d.name)}</b> <span class="pill">${esc(d.profile)}</span></div>
      <div class="muted">${esc(d.target)}/${esc(d.subtarget)} · ${esc(d.arch || 'arch unknown')} · OpenWrt ${esc(report.release)}</div>
      <button class="secondary" onclick="addDeviceRouterByIndex(${i})">Выбрать</button>
    </div>`).join('') : '<div class="muted">Ничего не найдено.</div>';
}

function searchDevices() {
  clearTimeout(searchTimer);
  const q = deviceSearch.value.trim();
  if (q.length < 2) {
    deviceResults.innerHTML = '';
    return;
  }
  searchTimer = setTimeout(async () => {
    deviceResults.innerHTML = '<div class="muted">Ищу...</div>';
    try {
      const report = await api('/api/devices?q=' + encodeURIComponent(q));
      renderDeviceResults(report);
    } catch (e) {
      deviceResults.innerHTML = '<div class="muted">' + esc(e.message) + '</div>';
    }
  }, 300);
}

function addSource() {
  cfg.package_sources = cfg.package_sources || [];
  cfg.package_sources.push({name:'custom', url:'', arch:'', packages:'', regex:'', enabled:true});
  renderSources();
  save();
}

async function save() {
  clearTimeout(saveTimer);
  pullForm();
  await api('/api/config', {method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify(cfg)});
  dirty = false;
  await load();
}

async function buildNow(router) {
  pullForm();
  await api('/api/config', {method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify(cfg)});
  await api('/api/build', {method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify({router, force:true})});
  setTimeout(load, 1000);
}

async function scanRepos() {
  pullForm();
  scan.innerHTML = 'Проверяю...';
  await api('/api/config', {method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify(cfg)});
  const report = await api('/api/scan-packages', {method:'POST'});
  scan.innerHTML = report.routers.map(r => `
    <div class="item">
      <b>${esc(r.router)} ${esc(r.release)}</b>
      <span class="muted">${esc(r.arch)}</span>
      ${r.sources.map(s => `
        <div>
          <b>${esc(s.name)}</b>
          ${s.packages.length ? s.packages.map(p => `<div><a href="${esc(p.url)}">${esc(p.file)}</a></div>`).join('') : '<div class="muted">Подходящих APK не найдено</div>'}
        </div>`).join('')}
    </div>`).join('');
}

async function load() {
  if (!dirty) cfg = await api('/api/config');
  const st = await api('/api/status');
  latest.textContent = 'Latest: ' + (st.latest_release || 'unknown');
  jobs.innerHTML = (st.jobs || []).map(j => `<div class="item"><b>${esc(j.router)} ${esc(j.release)}</b><span class="pill">${esc(j.status)}</span><span class="muted">${esc(j.updated_at)}</span>${j.output ? `<a href="${esc(j.output)}">firmware</a>` : ''}${j.error ? `<pre>${esc(j.error)}</pre>` : ''}<a href="${esc(j.log)}">log</a></div>`).join('');
  if (!dirty) bindConfig();
}
load();
setInterval(load, 15000);
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[{utc_now()}] {self.address_string()} {fmt % args}", flush=True)

    def send(self, status, body, content_type="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
        elif path == "/api/config":
            self.send(200, config())
        elif path == "/api/status":
            self.send(200, state())
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
        elif path in ["/api/overview", "/api/v1/overview", "/json/v1/overview.json"]:
            self.send(200, asu_overview())
        elif path.startswith("/logs/"):
            file_path = LOG_DIR / Path(path).name
            if file_path.exists():
                self.send(200, file_path.read_text(encoding="utf-8", errors="replace"), "text/plain; charset=utf-8")
            else:
                self.send(404, {"error": "not found"})
        elif path.startswith("/firmware/"):
            file_path = OUTPUT_DIR / Path(urllib.parse.unquote(path[len("/firmware/"):]))
            if file_path.is_dir():
                listing = []
                for p in sorted(file_path.iterdir()):
                    listing.append({"name": p.name, "url": path.rstrip("/") + "/" + urllib.parse.quote(p.name)})
                self.send(200, listing)
            elif file_path.exists():
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(file_path.stat().st_size))
                self.end_headers()
                with file_path.open("rb") as fh:
                    shutil.copyfileobj(fh, self.wfile)
            else:
                self.send(404, {"error": "not found"})
        elif path == "/api/asu":
            self.send(200, {
                "service": "local-openwrt-builder",
                "note": "Use /firmware/<router>/latest/<sysupgrade-file> as local sysupgrade URL, or set this base URL in Attended Sysupgrade for discovery-compatible clients.",
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
            threading.Thread(target=lambda: check_and_build(force=force, router_name=router), daemon=True).start()
            self.send(202, {"status": "queued"})
        elif path == "/api/scan-packages":
            self.send(200, scan_repositories())
        elif path in ["/api/v1/build", "/api/build-request"]:
            body = self.read_body()
            board = body.get("profile") or body.get("board") or body.get("target")
            router = None
            for item in config().get("routers", []):
                if board in [item.get("name"), item.get("profile")]:
                    router = item.get("name")
                    break
            threading.Thread(target=lambda: check_and_build(force=True, router_name=router), daemon=True).start()
            self.send(202, {
                "status": "queued",
                "detail": "Local build queued. Poll the web UI or /firmware/<router>/latest/ for the finished sysupgrade image.",
            })
        else:
            self.send(404, {"error": "not found"})


def main():
    ensure_dirs()
    cfg = config()
    scheduler.start()
    server = ThreadingHTTPServer((cfg.get("listen_host", "0.0.0.0"), int(cfg.get("listen_port", 8088))), Handler)
    print(f"OpenWrt Builder listening on http://{cfg.get('listen_host')}:{cfg.get('listen_port')}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
