"""Manage the local Docker stack with stdlib Python; preserve business secrets."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time

from scripts.prepare_cpu import MODEL_FILE, download_model


ROOT = Path(__file__).resolve().parents[1]
NETWORK = "aigc-models"
NAMES = {"api": "aigc-checker-server", "ollama": "aigc-ollama", "scorer": "aigc-cpu-scorer"}
GIB = 1024 ** 3


def say(message):
    print(message, flush=True)


def run(args, *, capture=False, check=True, env=None, cwd=ROOT):
    result = subprocess.run(args, cwd=cwd, env=env, text=True,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None)
    if check and result.returncode:
        # Do not echo command arguments or captured environment/inspection output.
        raise RuntimeError(f"命令执行失败：{' '.join(args[:2])}（退出码 {result.returncode}）")
    return result


def docker(*args, **kwargs):
    return run(["docker", *args], **kwargs)


def read_values(path, keys):
    """Parse only named single-line settings. Never execute dotenv as shell code."""
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, raw = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        if separator and key in keys:
            parts = shlex.split(raw, comments=True)
            if len(parts) > 1:
                raise ValueError(f"{path.name} 中 {key} 必须为单个值")
            values[key] = parts[0] if parts else ""
    return values


def deployment_config(root):
    example = root / "deploy.env.example"
    keys = {line.partition("=")[0] for line in example.read_text().splitlines()
            if line and not line.startswith("#") and "=" in line}
    defaults = read_values(example, keys)
    path = root / "deploy.env"
    if not path.exists():
        content = example.read_text()
        if (root / ".env").exists():
            model = read_values(root / ".env", {"OLLAMA_MODEL"}).get("OLLAMA_MODEL")
            if model and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]*", model):
                content = content.replace("OLLAMA_DEPLOY_MODEL=qwen3:4b", f"OLLAMA_DEPLOY_MODEL={model}")
        with path.open("x", encoding="utf-8") as stream:
            stream.write(content)
        say("已创建 deploy.env；沿用 .env 中已有的 Ollama 检测模型。")
    provider = read_values(root / ".env", {"DETECTION_PROVIDER"}).get("DETECTION_PROVIDER", "ollama") if (root / ".env").exists() else "ollama"
    return {**defaults, **read_values(path, keys), "DETECTION_PROVIDER": provider.strip().lower()}


def positive(value, key):
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise ValueError(f"{key} 必须为正整数或使用支持的 auto 默认值")
    return int(value)


def resources(config, cpus, memory):
    provider = config.get("DETECTION_PROVIDER", "ollama")
    if provider not in {"ollama", "bailian"}:
        raise ValueError(".env 的 DETECTION_PROVIDER 只能是 ollama 或 bailian")
    local = provider == "ollama"
    enabled = config["SCORER_ENABLED"].lower()
    if enabled not in {"true", "false"}:
        raise ValueError("SCORER_ENABLED 必须是 true 或 false")
    enabled = enabled == "true"

    def setting(key, default):
        return default if config[key] == "auto" else positive(config[key], key)

    ollama_cpu = setting("OLLAMA_DEPLOY_CPUS", min(2 if enabled else 3, max(1, cpus - 1))) if local else 0
    scorer_cpu = setting("SCORER_CPUS", min(2, max(1, cpus - ollama_cpu - 1)))
    default_memory = max(1, min(6, (memory // GIB - 4) // (2 if enabled and local else 1)))
    ollama_memory = setting("OLLAMA_DEPLOY_MEMORY_GB", default_memory) if local else 0
    scorer_memory = setting("SCORER_MEMORY_GB", default_memory)
    threads = setting("SCORER_THREADS", scorer_cpu)
    if ollama_cpu > cpus or enabled and (scorer_cpu > cpus or ollama_cpu + scorer_cpu > cpus):
        raise ValueError("模型 CPU 配额超过 Docker 可用核心数，请调整 deploy.env")
    total_memory = ollama_memory + (scorer_memory if enabled else 0)
    if total_memory * GIB > memory - 2 * GIB:
        raise ValueError("模型内存上限合计必须至少为业务和系统留下 2GiB，请调整 deploy.env")
    if enabled and threads > scorer_cpu:
        raise ValueError("SCORER_THREADS 不应大于 SCORER_CPUS")
    context = positive(config["SCORER_CONTEXT_TOKENS"], "SCORER_CONTEXT_TOKENS")
    stride = positive(config["SCORER_STRIDE_TOKENS"], "SCORER_STRIDE_TOKENS")
    if not 0 < stride < context or context < 4:
        raise ValueError("评分窗口需满足 0 < STRIDE < CONTEXT，且 CONTEXT >= 4")
    if positive(config["SCORER_MAX_TOKENS"], "SCORER_MAX_TOKENS") < 2:
        raise ValueError("SCORER_MAX_TOKENS 至少为 2")
    positive(config["SCORER_WAIT_SECONDS"], "SCORER_WAIT_SECONDS")
    if config["MODEL_SOURCE"] not in {"modelscope", "huggingface"}:
        raise ValueError("MODEL_SOURCE 必须是 modelscope 或 huggingface")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]*", config["OLLAMA_DEPLOY_MODEL"]):
        raise ValueError("OLLAMA_DEPLOY_MODEL 格式不正确")
    return {"enabled": enabled, "ollama_enabled": local, "ollama_cpu": ollama_cpu, "scorer_cpu": scorer_cpu,
            "ollama_memory": ollama_memory, "scorer_memory": scorer_memory, "threads": threads}


def inspect(name):
    result = docker("container", "inspect", name, capture=True, check=False)
    return json.loads(result.stdout)[0] if result.returncode == 0 else None


def ensure_network(name):
    item = inspect(name)
    if item is None:
        raise RuntimeError(f"容器 {name} 不存在，请先执行 bash manage.sh up")
    if not item.get("State", {}).get("Running"):
        raise RuntimeError(f"模型容器 {name} 未运行，请先执行 bash manage.sh up")
    if NETWORK not in item.get("NetworkSettings", {}).get("Networks", {}):
        docker("network", "connect", NETWORK, name)


def wait_scorer(timeout):
    deadline = time.monotonic() + timeout
    attempt = 0
    while time.monotonic() < deadline:
        item = inspect(NAMES["scorer"])
        state = item.get("State", {}) if item else {}
        if state.get("Status") in {"exited", "dead", "restarting"}:
            raise RuntimeError("评分服务启动失败，请执行 bash manage.sh logs scorer")
        if state.get("Status") == "running":
            result = docker("exec", NAMES["scorer"], "python", "-c",
                            "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8091/health', timeout=3)",
                            capture=True, check=False)
            if result.returncode == 0:
                return
        if attempt % 6 == 0:
            say("等待评分模型加载和 /health 就绪…")
        attempt += 1
        time.sleep(5)
    raise RuntimeError("评分服务加载超时，请执行 bash manage.sh logs scorer")


def deploy_scorer(root, config, limits):
    name = NAMES["scorer"]
    old = inspect(name)
    backup = f"{name}-backup-{time.time_ns()}"
    if old:
        if old["State"].get("Running"):
            docker("stop", name)
        docker("rename", name, backup)
    try:
        args = ["run", "-d", "--name", name, "--restart", "unless-stopped", "--network", NETWORK,
                "--cpus", str(limits["scorer_cpu"]), "--memory", f"{limits['scorer_memory']}g",
                "--memory-swap", f"{limits['scorer_memory']}g",
                "-p", "127.0.0.1:8091:8091", "-v", f"{root / 'models'}:/models:ro",
                "--log-driver", "json-file", "--log-opt", "max-size=10m", "--log-opt", "max-file=3"]
        env = {"SCORER_MODEL_PATH": f"/models/{MODEL_FILE}", "SCORER_THREADS": str(limits["threads"]),
               "OPENBLAS_NUM_THREADS": str(limits["threads"]), "OMP_NUM_THREADS": str(limits["threads"]),
               **{key: config[key] for key in ("SCORER_CONTEXT_TOKENS", "SCORER_STRIDE_TOKENS", "SCORER_MAX_TOKENS")}}
        for key, value in env.items():
            args.extend(["-e", f"{key}={value}"])
        docker(*args, "aigc-cpu-scorer:0.1")
        wait_scorer(int(config["SCORER_WAIT_SECONDS"]))
    except Exception:
        # Only our newly created stateless scorer is removed. Model files stay mounted read-only.
        docker("rm", "-f", name, capture=True, check=False)
        if old:
            docker("rename", backup, name)
            if old["State"].get("Running"):
                docker("start", name)
        raise
    if old:
        docker("rm", backup)


def sync_app_env(root, config, limits):
    """Change routing only after dependencies are ready; preserve every unrelated line."""
    path = root / ".env"
    original = path.read_text(encoding="utf-8")
    existing = read_values(path, {"OLLAMA_TIMEOUT", "PROBABILITY_TIMEOUT", "OLLAMA_JSON_NUM_CTX",
                                  "OLLAMA_JSON_NUM_PREDICT", "AZURE_DEPLOYMENT", "AZURE_REVIEW_DEPLOYMENT",
                                  "PRO_REVIEW_SOURCE", "PRO_REVIEW_MODEL", "AZURE_API_MODE", "AZURE_OPENAI_DEPLOYMENT"})
    review_model = existing.get("AZURE_REVIEW_DEPLOYMENT") or (
        existing.get("PRO_REVIEW_MODEL") if existing.get("PRO_REVIEW_SOURCE", "azure") == "azure" else None
    ) or existing.get("AZURE_OPENAI_DEPLOYMENT") or existing.get("AZURE_DEPLOYMENT") or "gpt-4o"
    overrides = {
        "DETECTION_PROVIDER": config.get("DETECTION_PROVIDER", "ollama"),
        "API_SOURCE": config.get("DETECTION_PROVIDER", "ollama"),
        "PRO_REVIEW_SOURCE": "azure", "AZURE_REVIEW_DEPLOYMENT": review_model, "PRO_REVIEW_MODEL": review_model,
        "AZURE_API_MODE": existing.get("AZURE_API_MODE") or "v1",
        "OLLAMA_DOCKER_NETWORK": NETWORK,
        "SCAN_CHUNK_CONCURRENCY": "1", "PROBABILITY_ENABLED": str(limits["enabled"]).lower(),
        "PROBABILITY_BASE_URL": "http://aigc-cpu-scorer:8091",
        "OLLAMA_TIMEOUT": existing.get("OLLAMA_TIMEOUT") or "600",
        "PROBABILITY_TIMEOUT": existing.get("PROBABILITY_TIMEOUT") or "600",
        "OLLAMA_JSON_NUM_CTX": existing.get("OLLAMA_JSON_NUM_CTX") or "2048",
        "OLLAMA_JSON_NUM_PREDICT": existing.get("OLLAMA_JSON_NUM_PREDICT") or "256",
    }
    if limits["ollama_enabled"]:
        overrides.update({"OLLAMA_BASE_URL": "http://aigc-ollama:11434",
                          "OLLAMA_MODEL": config["OLLAMA_DEPLOY_MODEL"],
                          "OLLAMA_REWRITE_MODEL": config["OLLAMA_DEPLOY_MODEL"],
                          "OLLAMA_NUM_THREAD": str(limits["ollama_cpu"]), "OLLAMA_MAX_CONCURRENCY": "1"})
    lines, seen = [], set()
    for line in original.splitlines():
        key = line.partition("=")[0].strip().removeprefix("export ").strip()
        if "=" in line and key in overrides:
            if key not in seen:
                lines.append(f"{key}={overrides[key]}")
                seen.add(key)
        else:
            lines.append(line)
    lines.extend(f"{key}={value}" for key, value in overrides.items() if key not in seen)
    updated = "\n".join(lines) + "\n"
    if updated != original:
        backup = root / f".env.backup.{datetime.now():%Y%m%d%H%M%S}.{time.time_ns()}"
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(original)
        fd, temporary = tempfile.mkstemp(prefix=".env.backup.write-", dir=root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(updated)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        say(f"已更新本地路由和概率开关；原配置备份为 {backup.name}（权限 0600）。")


def verify_stack(*, score=False):
    # Run inside the API to verify Docker DNS/routing, not merely host-bound ports.
    code = '''
import asyncio, json, os, urllib.request
from services.probability_client import PROBABILITY_ENABLED, measure_probability
from services.model_client import DETECTION_PROVIDER, default_model
def get(url):
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.load(response)
get('http://127.0.0.1:8027/health')
report = {'api': 'ok', 'provider': DETECTION_PROVIDER, 'model': default_model(), 'probability_enabled': PROBABILITY_ENABLED}
if DETECTION_PROVIDER == 'ollama':
    tags = get(os.environ['OLLAMA_BASE_URL'].rstrip('/') + '/api/tags')
    names = {item['name'] for item in tags['models']}
    assert default_model() in names or default_model() + ':latest' in names, 'Ollama model missing'
    report['ollama'] = 'ok'
else:
    from services.bailian_client import validate_config
    validate_config()
    report['bailian'] = 'configured_not_called'
if PROBABILITY_ENABLED:
    health = get(os.environ['PROBABILITY_BASE_URL'].rstrip('/') + '/health')
    assert health['status'] == 'ok', 'Scorer not ready'
    report['scorer'] = 'ok'
if __RUN_SCORE__:
    assert PROBABILITY_ENABLED, 'Scorer is disabled; enable SCORER_ENABLED then run manage.sh up'
    result = asyncio.run(measure_probability('今天下班时突然下雨，我在便利店买了一把伞，走到家门口雨又停了。'))
    assert result['status'] == 'measured', 'Original-text scoring failed: ' + result.get('error_type', 'unknown')
    report['probability'] = result
print(json.dumps(report, ensure_ascii=False, indent=2))
'''.replace("__RUN_SCORE__", repr(score))
    docker("exec", NAMES["api"], "python", "-c", code)


def up(root, config, limits, target, *, build=True):
    if not (root / ".env").is_file():
        raise ValueError("缺少 .env，请先填写现有业务配置和 Token")
    if not limits["ollama_enabled"]:
        if target == "ollama":
            raise ValueError("当前使用百炼；如需启动 Ollama，先将 .env 的 DETECTION_PROVIDER 改为 ollama")
        values = read_values(root / ".env", {"BAILIAN_API_KEY", "BAILIAN_BASE_URL"})
        if not values.get("BAILIAN_API_KEY") or values["BAILIAN_API_KEY"].startswith(("replace_", "REPLACE_")):
            raise ValueError("百炼模式需要在 .env 填写 BAILIAN_API_KEY")
        if values.get("BAILIAN_BASE_URL") and not values["BAILIAN_BASE_URL"].startswith("https://"):
            raise ValueError("BAILIAN_BASE_URL 必须为 HTTPS 地址")
    if target == "scorer" and not limits["enabled"]:
        raise ValueError("deploy.env 的 SCORER_ENABLED=false；改为 true 后执行 up")
    if not build:
        if target != "api":
            raise ValueError("reload 仅支持业务 API；模型服务更新请使用 update scorer/ollama")
        if docker("image", "inspect", "aigc-checker-server:latest", capture=True, check=False).returncode:
            raise ValueError("现有业务镜像不存在，请先执行 bash manage.sh update api")
        say("复用已有业务镜像，仅加载配置；源码变更请使用 update api。")
    model_services = target in {"all", "scorer"} and limits["enabled"]
    if model_services:
        # Existing download is verified and reused; no second download on updates.
        download_model(root, config["MODEL_SOURCE"])
    # Build selected images before stopping/replacing any containers.
    if model_services:
        docker("build", "--build-arg", f"BUILD_JOBS={limits['scorer_cpu']}", "-f",
               "cpu_scoring/Dockerfile", "-t", "aigc-cpu-scorer:0.1", ".")
    if target in {"all", "api"} and build:
        docker("build", "-t", "aigc-checker-server:latest", ".")
    if docker("network", "inspect", NETWORK, capture=True, check=False).returncode:
        docker("network", "create", NETWORK)
    if target in {"all", "ollama"} and limits["ollama_enabled"]:
        say("准备 Ollama；复用已有容器、模型卷和已下载的模型。")
        run(["bash", "scripts/deploy_ollama.sh"], env={**os.environ,
            "OLLAMA_DEPLOY_MODEL": config["OLLAMA_DEPLOY_MODEL"], "OLLAMA_IMAGE": config["OLLAMA_IMAGE"],
            "OLLAMA_DEPLOY_CPUS": str(limits["ollama_cpu"]),
            "OLLAMA_DEPLOY_MEMORY_GB": str(limits["ollama_memory"]),
            "OLLAMA_DEPLOY_SKIP_PULL_IF_PRESENT": "true"})
    if model_services:
        deploy_scorer(root, config, limits)
    if target in {"all", "api"}:
        if limits["ollama_enabled"]:
            ensure_network(NAMES["ollama"])
        if limits["enabled"]:
            ensure_network(NAMES["scorer"])
            wait_scorer(int(config["SCORER_WAIT_SECONDS"]))
        sync_app_env(root, config, limits)
        run(["bash", "disploy.sh"], env={**os.environ, "SKIP_GIT_PULL": "1", "SKIP_DOCKER_BUILD": "1",
                                         "OLLAMA_DOCKER_NETWORK": NETWORK})
        verify_stack()
        # Apply API configuration before stopping a scorer it used to depend on.
        if not limits["enabled"] and inspect(NAMES["scorer"]):
            docker("stop", NAMES["scorer"])
        if not limits["ollama_enabled"] and inspect(NAMES["ollama"]):
            docker("stop", NAMES["ollama"])
    else:
        say("模型服务已更新；如需同步模型名或概率开关，请继续执行 bash manage.sh update api。")
    say("部署完成。bash manage.sh check --score 可实际测量一次 PPL；不会调用云端复核。")


def main():
    parser = argparse.ArgumentParser(description="统一管理业务 API、Ollama 与可选原文评分服务。")
    parser.add_argument("command", choices=["up", "update", "reload", "status", "logs", "restart", "stop", "check", "download", "config"])
    parser.add_argument("service", nargs="?", choices=["all", "api", "ollama", "scorer"])
    parser.add_argument("--score", action="store_true", help="check 时实际测量短文本的 PPL，不调用云端模型")
    parser.add_argument("-f", "--follow", action="store_true", help="logs 时持续跟随日志")
    args = parser.parse_args()
    if args.score and args.command != "check" or args.follow and args.command != "logs":
        parser.error("--score 仅用于 check；--follow 仅用于 logs")
    if args.command == "reload" and args.service not in {None, "api"}:
        parser.error("reload 仅支持 api；更新模型服务请使用 update scorer/ollama")
    os.chdir(ROOT)
    if args.command == "download":
        config = deployment_config(ROOT)
        download_model(ROOT, config["MODEL_SOURCE"])
        return
    raw = docker("info", "--format", "{{.NCPU}} {{.MemTotal}}", capture=True).stdout.split()
    cpus, memory = map(int, raw)
    target = args.service or ("api" if args.command in {"logs", "reload"} else "all")
    if args.command in {"up", "update", "reload", "config"}:
        config = deployment_config(ROOT)
        limits = resources(config, cpus, memory)
        say(f"Docker：{cpus} 核 / {memory / GIB:.1f}GiB；检测提供方：{config['DETECTION_PROVIDER']}。")
        if limits["ollama_enabled"]:
            say(f"Ollama：{limits['ollama_cpu']} 核 / {limits['ollama_memory']}GiB。")
        say(f"评分服务：{limits['scorer_cpu']} 核 / {limits['scorer_memory']}GiB。" if limits["enabled"] else "评分服务：关闭。")
        if args.command == "config":
            say(json.dumps(config, ensure_ascii=False, indent=2))
        else:
            up(ROOT, config, limits, target, build=args.command != "reload")
    elif args.command == "check":
        verify_stack(score=args.score)
    else:
        names = list(NAMES.values()) if target == "all" else [NAMES[target]]
        if args.command == "status":
            filters = [part for name in names for part in ("--filter", f"name=^/{name}$")]
            docker("ps", "-a", *filters, "--format", "table {{.Names}}\t{{.Status}}\t{{.Ports}}")
        elif args.command == "logs":
            if target == "all" and args.follow:
                raise ValueError("持续日志请指定 api、ollama 或 scorer")
            for name in names:
                say(name)
                docker("logs", "--tail", "100", *(["--follow"] if args.follow else []), name)
        else:
            # Stop API first; restart models first, then API. Do not start disabled containers.
            if args.command == "stop":
                names = [NAMES[key] for key in ("api", "scorer", "ollama") if NAMES[key] in names]
            else:
                names = [NAMES[key] for key in ("ollama", "scorer", "api") if NAMES[key] in names]
            for name in names:
                item = inspect(name)
                if item and (args.command == "stop" or item["State"].get("Running")):
                    docker(args.command, name)
            if args.command == "restart":
                say("已重启运行中的服务；仅修改 .env 请用 reload api，源码更新用 update api。停止后的整套恢复使用 up。")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, OSError, subprocess.CalledProcessError) as exc:
        say(f"错误：{exc}")
        sys.exit(1)
