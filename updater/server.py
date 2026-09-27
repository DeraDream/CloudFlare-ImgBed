#!/usr/bin/env python3
import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = os.getenv("UPDATE_HOST", "0.0.0.0")
PORT = int(os.getenv("UPDATE_PORT", "8081"))
PROJECT_DIR = Path(os.getenv("PROJECT_DIR", "/opt/docker/cloudflare-imgbed")).resolve()
BRANCH = os.getenv("REPO_BRANCH", "main")
EXPECTED_REPO = os.getenv("EXPECTED_REPO", "DeraDream/CloudFlare-ImgBed")
REPO_URL = os.getenv("REPO_URL", "https://github.com/DeraDream/CloudFlare-ImgBed.git")
COMPOSE_FILE = PROJECT_DIR / "docker-compose.yml"
DATA_DIR = PROJECT_DIR / "data"
STATE_FILE = DATA_DIR / "update-state.json"
LOG_FILE = DATA_DIR / "update-agent.log"

lock = threading.Lock()
update_thread = None
state = {
    "updating": False,
    "stage": "idle",
    "message": "",
    "startedAt": None,
    "finishedAt": None,
    "success": None,
    "targetImage": None,
    "handoffUntil": None,
}

# updater 自身会在成功升级后被重建，因此把最后结果恢复到内存，
# 让重启后的 Bot 仍能获得“升级成功/失败”状态并主动反馈。
if STATE_FILE.exists():
    try:
        saved_state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(saved_state, dict):
            state.update(saved_state)
    except Exception:
        pass


def now():
    return int(time.time())


def log(message):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    print(line, flush=True)
    with LOG_FILE.open("a", encoding="utf-8") as fp:
        fp.write(line + "\n")


def persist_state():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def run(cmd, timeout=1800, check=True):
    log("$ " + " ".join(str(x) for x in cmd))
    proc = subprocess.run(
        [str(x) for x in cmd],
        cwd=str(PROJECT_DIR),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )
    output = (proc.stdout or "").strip()
    if output:
        for line in output.splitlines()[-80:]:
            log(line)
    if check and proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(cmd)}")
    return output


def git(*args, timeout=120):
    return run(["git", "-C", str(PROJECT_DIR), *args], timeout=timeout)


def fetch_latest():
    # 固定通过公开 HTTPS 地址拉取，避免部署仓库 origin 使用 git@github.com 时依赖 SSH Key。
    git(
        "fetch",
        "--quiet",
        REPO_URL,
        f"+refs/heads/{BRANCH}:refs/remotes/origin/{BRANCH}",
        timeout=180,
    )


def read_version(ref=None):
    try:
        if ref:
            value = git("show", f"{ref}:VERSION", timeout=30).splitlines()[0].strip()
        else:
            value = (PROJECT_DIR / "VERSION").read_text(encoding="utf-8").strip()
        return value or "unknown"
    except Exception:
        return "unknown"


def validate_repo():
    if not (PROJECT_DIR / ".git").exists():
        raise RuntimeError(f"{PROJECT_DIR} is not a git checkout")
    remote = git("remote", "get-url", "origin", timeout=30).strip()
    if EXPECTED_REPO and EXPECTED_REPO.lower() not in remote.lower():
        raise RuntimeError(f"unexpected git origin: {remote}")
    if not COMPOSE_FILE.exists():
        raise RuntimeError(f"compose file not found: {COMPOSE_FILE}")


def status_payload(fetch=True):
    global update_thread

    with lock:
        snapshot = dict(state)

    # 如果 updater 容器曾在升级过程中被重启，持久化文件可能仍是 updating=true，
    # 但新进程里已经没有执行中的升级线程。此时自动恢复，不再永久卡在 deploy。
    with lock:
        stale_updating = bool(state.get("updating")) and not (
            update_thread is not None and update_thread.is_alive()
        )

    try:
        validate_repo()
        if fetch or stale_updating:
            fetch_latest()

        current_commit = git("rev-parse", "HEAD", timeout=30).strip()
        latest_commit = git("rev-parse", f"origin/{BRANCH}", timeout=30).strip()

        if stale_updating:
            running = set(
                run(
                    [
                        "docker", "compose", "-f", str(COMPOSE_FILE),
                        "ps", "--services", "--filter", "status=running"
                    ],
                    timeout=30,
                    check=False,
                ).splitlines()
            )

            container_id = run(
                [
                    "docker", "compose", "-f", str(COMPOSE_FILE),
                    "ps", "-q", "imgbed"
                ],
                timeout=30,
                check=False,
            ).strip()
            running_image = ""
            if container_id:
                running_image = run(
                    ["docker", "inspect", "-f", "{{.Image}}", container_id],
                    timeout=30,
                    check=False,
                ).strip()

            target_image = str(state.get("targetImage") or "").strip()
            handoff_until = int(state.get("handoffUntil") or 0)
            image_matches = (not target_image) or running_image == target_image
            deploy_ok = (
                current_commit == latest_commit
                and "imgbed" in running
                and image_matches
            )

            if deploy_ok:
                with lock:
                    state.update({
                        "updating": False,
                        "stage": "done",
                        "message": "升级完成，ImgBed / Telegram Bot / 更新器已全部应用新版本",
                        "finishedAt": now(),
                        "success": True,
                        "handoffUntil": None,
                    })
                    persist_state()
            elif handoff_until and now() < handoff_until:
                # helper 正在接管当前容器，暂时维持 updating=true，避免误判失败。
                pass
            else:
                with lock:
                    state.update({
                        "updating": False,
                        "stage": "failed",
                        "message": "升级接管超时或新镜像未成功运行，请检查更新日志",
                        "finishedAt": now(),
                        "success": False,
                        "handoffUntil": None,
                    })
                    persist_state()

        with lock:
            snapshot = dict(state)
        current_version = read_version()
        latest_version = read_version(f"origin/{BRANCH}")
        dirty = bool(git("status", "--porcelain", "--untracked-files=no", timeout=30).strip())
        snapshot.update({
            "ok": True,
            "currentVersion": current_version,
            "latestVersion": latest_version,
            "currentCommit": current_commit,
            "latestCommit": latest_commit,
            "currentShort": current_commit[:8],
            "latestShort": latest_commit[:8],
            "updateAvailable": current_commit != latest_commit,
            "dirty": dirty,
            "branch": BRANCH,
            "repo": EXPECTED_REPO,
        })
    except Exception as exc:
        snapshot.update({"ok": False, "error": str(exc)})
    if LOG_FILE.exists():
        try:
            lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
            snapshot["logTail"] = lines[-30:]
        except Exception:
            pass
    return snapshot


def do_update():
    with lock:
        if state["updating"]:
            return
        state.update({
            "updating": True,
            "stage": "starting",
            "message": "正在准备升级",
            "startedAt": now(),
            "finishedAt": None,
            "success": None,
            "targetImage": None,
            "handoffUntil": None,
        })
        persist_state()

    previous = None
    try:
        validate_repo()
        previous = git("rev-parse", "HEAD", timeout=30).strip()

        with lock:
            state.update({"stage": "fetch", "message": "正在获取 GitHub 最新版本"})
            persist_state()
        fetch_latest()

        dirty = git("status", "--porcelain", "--untracked-files=no", timeout=30).strip()
        if dirty:
            raise RuntimeError("检测到部署目录存在已修改的 Git 跟踪文件，为避免覆盖本地改动，已拒绝自动升级")

        latest = git("rev-parse", f"origin/{BRANCH}", timeout=30).strip()
        if previous == latest:
            with lock:
                state.update({
                    "updating": False,
                    "stage": "done",
                    "message": "当前已经是最新版本",
                    "finishedAt": now(),
                    "success": True,
                })
                persist_state()
            return

        with lock:
            state.update({"stage": "checkout", "message": "正在切换到最新代码"})
            persist_state()
        # 部署目录专用于运行本项目；data/.env 都是未跟踪/忽略内容，不受 reset 影响。
        git("reset", "--hard", f"origin/{BRANCH}", timeout=60)

        with lock:
            state.update({
                "stage": "build",
                "message": "正在构建新版 ImgBed 一体化容器（包含 Telegram Bot 与更新器）"
            })
            persist_state()

        run([
            "docker", "compose", "-f", str(COMPOSE_FILE),
            "build", "--pull", "imgbed"
        ], timeout=3600)

        image_names = run([
            "docker", "compose", "-f", str(COMPOSE_FILE),
            "config", "--images"
        ], timeout=30).splitlines()
        image_name = image_names[0].strip() if image_names else ""
        if not image_name:
            raise RuntimeError("cannot resolve configured imgbed image name")

        target_image = run([
            "docker", "image", "inspect", "-f", "{{.Id}}", image_name
        ], timeout=30).strip()
        if not target_image:
            raise RuntimeError("cannot resolve newly built imgbed image")

        with lock:
            state.update({
                "stage": "deploy",
                "message": "正在重启 ImgBed 一体化容器并应用新版本",
                "targetImage": target_image,
                "handoffUntil": now() + 120,
            })
            persist_state()

        # 当前 updater 已经运行在 imgbed 容器内部，不能直接由自己执行
        # "docker compose up imgbed" 重建自己，否则命令进程会被一起杀死。
        # 使用刚构建的新镜像启动一个临时 helper，helper 不属于 Compose 项目，
        # 即使当前 imgbed 被停止，它仍能继续完成单容器重建。
        helper_name = f"imgbed-update-helper-{now()}"
        helper_script = (
            "sleep 3; "
            f"cd {str(PROJECT_DIR)!r}; "
            f"docker compose -f {str(COMPOSE_FILE)!r} "
            "up -d --no-deps --no-build --force-recreate --remove-orphans imgbed"
        )
        run([
            "docker", "run", "--rm", "-d",
            "--name", helper_name,
            "-v", "/var/run/docker.sock:/var/run/docker.sock",
            "-v", f"{PROJECT_DIR}:{PROJECT_DIR}",
            "-e", f"PROJECT_DIR={PROJECT_DIR}",
            "-w", str(PROJECT_DIR),
            target_image,
            "sh", "-c", helper_script,
        ], timeout=60)
        log(f"scheduled combined imgbed refresh via helper {helper_name}")

    except Exception as exc:
        log(f"UPDATE FAILED: {exc}")
        with lock:
            state.update({
                "updating": False,
                "stage": "failed",
                "message": str(exc),
                "finishedAt": now(),
                "success": False,
                "previousCommit": previous,
            })
            persist_state()


class Handler(BaseHTTPRequestHandler):
    server_version = "ImgBedUpdater/1.0"

    def _json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        log("http " + (fmt % args))

    def do_GET(self):
        if self.path.startswith("/status"):
            self._json(200, status_payload(fetch=True))
            return
        if self.path.startswith("/health"):
            self._json(200, {"ok": True})
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        global update_thread
        if not self.path.startswith("/update"):
            self._json(404, {"ok": False, "error": "not found"})
            return

        # 先调用一次状态恢复，避免容器重启后遗留 updating=true 永久阻塞下一次升级。
        current = status_payload(fetch=True)
        if current.get("updating"):
            self._json(409, {"ok": False, "error": "update already running", **current})
            return

        update_thread = threading.Thread(target=do_update, daemon=True)
        update_thread.start()
        self._json(202, {
            "ok": True,
            "accepted": True,
            "message": "升级任务已开始，ImgBed 一体化容器将短暂重启",
        })


if __name__ == "__main__":
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log(f"ImgBed updater listening on {HOST}:{PORT}, project={PROJECT_DIR}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
