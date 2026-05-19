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
    .brand-mark { width: 40px; height: 40px; border-radius: 8px; background: linear-gradient(135deg, #1a73e8, #34a853); color: white; display: grid; place-items: center; font-weight: 800; letter-spacing: 0; box-shadow: 0 8px 18px rgba(26,115,232,.22); }
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
    .modal-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding-bottom: 4px; }
    .modal-head h2 { margin: 0; font-size: 19px; }
    textarea.packages { min-height: 190px; font-family: "Roboto Mono", ui-monospace, SFMono-Regular, Consolas, monospace; font-size: 13px; line-height: 1.5; }
    .muted { color: var(--muted); font-size: 13px; }
    .pill { display: inline-flex; align-items: center; background: var(--primary-soft); color: #174ea6; border-radius: 999px; padding: 4px 9px; font-size: 12px; font-weight: 700; }
    .status-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 6px; background: var(--success); }
    code { background: var(--surface-2); border: 1px solid var(--border); border-radius: 6px; padding: 2px 6px; }
    pre { white-space: pre-wrap; background: #202124; color: #e8eaed; padding: 14px; border-radius: 8px; max-height: 260px; overflow: auto; }
    @media (max-width: 860px) { .grid { grid-template-columns: 1fr; } .topbar, main { padding-left: 16px; padding-right: 16px; } .topbar { display: grid; height: auto; padding-top: 14px; padding-bottom: 14px; } .toolbar, .section-head { align-items: stretch; } }
  </style>
</head>
<body>
<header>
  <div class="topbar">
    <div class="brand">
      <div class="brand-mark">OW</div>
      <div>
        <h1>OpenWrt Builder</h1>
        <div class="brand-subtitle">Локальная сборка прошивок для OpenWrt 25.x</div>
      </div>
    </div>
    <div class="row">
      <span id="latest" class="pill"><span class="status-dot"></span>...</span>
      <button onclick="buildNow()">Собрать сейчас</button>
    </div>
  </div>
</header>
<main>
  <section>
    <div class="section-head">
      <h2>Основные настройки</h2>
    </div>
    <div class="grid">
      <label>Публичный URL сервера <input id="public_base_url" oninput="scheduleSave()"></label>
      <label>Ветка релизов <input id="release_branch_prefix" placeholder="25." oninput="scheduleSave()"></label>
      <label>Проверять каждые, минут <input id="check_interval_minutes" type="number" min="5" oninput="scheduleSave()"></label>
      <label>Разрешить untrusted APK <select id="allow_untrusted_apk" onchange="scheduleSave()"><option value="true">Да</option><option value="false">Нет</option></select></label>
    </div>
  </section>
  <section>
    <div class="section-head">
      <h2>Роутеры</h2>
      <button class="secondary" onclick="openRouterModal()">Добавить роутер</button>
    </div>
    <div id="routers"></div>
  </section>
  <section>
    <div class="section-head">
      <h2>Внешние APK / репозитории</h2>
      <button class="secondary" onclick="addSource()">Добавить источник</button>
    </div>
    <div id="sources"></div>
  </section>
  <section>
    <div class="toolbar">
      <div class="row">
        <button onclick="save()">Сохранить</button>
        <button class="secondary" onclick="load()">Обновить статус</button>
        <button class="secondary" onclick="scanRepos()">Проверить репозитории</button>
      </div>
      <p class="muted">Sysupgrade server: <code id="sysurl"></code></p>
    </div>
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
<div id="routerModal" class="modal-backdrop">
  <div class="modal">
    <div class="modal-head">
      <h2 id="routerModalTitle">Роутер</h2>
      <button class="secondary" onclick="closeRouterModal()">Закрыть</button>
    </div>
    <label>Поиск модели как в Firmware Selector <input id="modalDeviceSearch" placeholder="Например: Cudy WR3000, GL-MT6000, Archer C7" oninput="searchDevices()"></label>
    <div id="modalDeviceResults"></div>
    <div class="grid">
      <label>Название <input id="routerName"></label>
      <label>Target <input id="routerTarget" placeholder="mediatek"></label>
      <label>Subtarget <input id="routerSubtarget" placeholder="filogic"></label>
      <label>Profile <input id="routerProfile" placeholder="cudy_wr3000-v1"></label>
    </div>
    <div class="grid">
      <label>Arch APK <input id="routerArch" placeholder="aarch64_cortex-a53"></label>
      <label>Включен <select id="routerEnabled"><option value="true">Да</option><option value="false">Нет</option></select></label>
    </div>
    <label>Пакеты прошивки для этого роутера <textarea id="routerPackages" class="packages" spellcheck="false"></textarea></label>
    <div class="row">
      <button onclick="saveRouterModal()">Сохранить роутер</button>
      <button class="secondary" onclick="closeRouterModal()">Отмена</button>
    </div>
  </div>
</div>
<script>
let cfg = {};
let searchTimer = null;
let saveTimer = null;
let dirty = false;
let deviceResultCache = [];
let editingRouterIndex = null;

async function api(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

function renderRouters() {
  const list = cfg.routers || [];
  if (!list.length) {
    routers.innerHTML = '<div class="item"><b>Роутеры еще не добавлены</b><div class="muted">Нажмите «Добавить роутер», найдите модель и сохраните профиль сборки.</div></div>';
    return;
  }
  routers.innerHTML = `
    <div class="table-wrap">
    <table>
      <thead><tr><th>Название</th><th>Profile</th><th>Target</th><th>Arch</th><th></th></tr></thead>
      <tbody>
        ${list.map((r, i) => `
          <tr>
            <td><b>${esc(r.name || 'router')}</b><div class="muted">${r.enabled === false ? 'выключен' : 'включен'}</div></td>
            <td>${esc(r.profile || '')}</td>
            <td>${esc(r.target || '')}/${esc(r.subtarget || '')}</td>
            <td>${esc(r.arch || '')}</td>
            <td><div class="actions">
              <button class="secondary" onclick="openRouterModal(${i})">Редактировать</button>
              <button class="secondary" onclick="buildRouterByIndex(${i})">Собрать</button>
              <button class="danger" onclick="deleteRouter(${i})">Удалить</button>
            </div></td>
          </tr>`).join('')}
      </tbody>
    </table>
    </div>`;
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
  routerModalTitle.textContent = index === null ? 'Добавить роутер' : 'Редактировать роутер';
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
  const safeName = device.name || device.profile || 'router';
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

function renderDeviceResults(report) {
  if (report.error) {
    modalDeviceResults.innerHTML = '<div class="item"><b>Не удалось загрузить список устройств</b><div class="muted">' + esc(report.error) + '</div></div>';
    return;
  }
  const devices = report.devices || [];
  deviceResultCache = devices;
  modalDeviceResults.innerHTML = devices.length ? devices.map((d, i) => `
    <div class="item">
      <div><b>${esc(d.name)}</b> <span class="pill">${esc(d.profile)}</span></div>
      <div class="muted">${esc(d.target)}/${esc(d.subtarget)} · ${esc(d.arch || 'arch unknown')} · OpenWrt ${esc(report.release)}</div>
      <button class="secondary" onclick="addDeviceRouterByIndex(${i})">Заполнить поля</button>
    </div>`).join('') : '<div class="muted">Ничего не найдено.</div>';
}

function searchDevices() {
  clearTimeout(searchTimer);
  const q = modalDeviceSearch.value.trim();
  if (q.length < 2) {
    modalDeviceResults.innerHTML = '';
    return;
  }
  searchTimer = setTimeout(async () => {
    modalDeviceResults.innerHTML = '<div class="muted">Ищу...</div>';
    try {
      const report = await api('/api/devices?q=' + encodeURIComponent(q));
      renderDeviceResults(report);
    } catch (e) {
      modalDeviceResults.innerHTML = '<div class="muted">' + esc(e.message) + '</div>';
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
