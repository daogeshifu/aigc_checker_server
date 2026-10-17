"""Prepare a separate CPU configuration and checksum-verified GGUF model."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
import secrets
import subprocess


ROOT = Path(__file__).resolve().parents[1]
MODEL_FILE = "Qwen3-4B-Q4_K_M.gguf"
MODEL_REVISION = "bc640142c66e1fdd12af0bd68f40445458f3869b"
MODEL_SHA256 = "7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5"
MODEL_URL = f"https://huggingface.co/Qwen/Qwen3-4B-GGUF/resolve/{MODEL_REVISION}/{MODEL_FILE}"
MODELSCOPE_URL = f"https://modelscope.cn/models/Qwen/Qwen3-4B-GGUF/resolve/master/{MODEL_FILE}"

CPU_OVERRIDES = {
    "DETECTION_PROVIDER": "ollama",
    "API_SOURCE": "ollama",
    "FREE_API_TOKENS": "",
    "PRO_API_TOKENS": "",
    "FREE_MAX_CHARS": "3000",
    "PRO_MAX_CHARS": "6000",
    "OLLAMA_BASE_URL": "http://cpu-ollama:11434",
    "OLLAMA_MODEL": "cpu-qwen4b",
    "OLLAMA_REWRITE_MODEL": "cpu-qwen4b",
    "OLLAMA_TIMEOUT": "600",
    "OLLAMA_NUM_THREAD": "4",
    "OLLAMA_MAX_CONCURRENCY": "1",
    "SCAN_CHUNK_CONCURRENCY": "1",
    "OLLAMA_JSON_NUM_CTX": "2048",
    "OLLAMA_JSON_NUM_PREDICT": "256",
    "OLLAMA_DISABLE_THINKING": "true",
    "REMOTE_REVIEW_ENABLED": "false",
    "PROBABILITY_ENABLED": "true",
    "PROBABILITY_BASE_URL": "http://cpu-scorer:8091",
    "PROBABILITY_TIMEOUT": "600",
    "SCORER_THREADS": "4",
    "SCORER_CONTEXT_TOKENS": "1024",
    "SCORER_STRIDE_TOKENS": "512",
}


def prepare_env(root: Path) -> Path:
    target = root / ".env.cpu"
    if target.exists():
        print("保留已有 .env.cpu，未覆盖配置。")
        return target
    source = root / ".env"
    if not source.exists():
        source = root / ".env.example"
    original = source.read_text(encoding="utf-8")
    matches = re.findall(r"^[ \t]*API_TOKEN[ \t]*=[ \t]*(.*?)[ \t]*$", original, re.MULTILINE)
    token = matches[-1].strip("\"'") if matches else ""
    if not token or token == "aigcchecker":
        token = secrets.token_hex(24)
    overrides = {**CPU_OVERRIDES, "API_TOKEN": token}
    lines = [line for line in original.splitlines()
             if not any(re.match(rf"^{re.escape(key)}\s*=", line) for key in overrides)]
    content = "\n".join(lines) + "\n\n# Ubuntu 8-core / 32GB CPU profile\n"
    content += "\n".join(f"{key}={value}" for key, value in overrides.items()) + "\n"
    # Exclusive creation and mode 0600, including when the parent umask is permissive.
    import os
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(content)
    print("已生成 .env.cpu；原 .env 未修改，Token 未打印。")
    return target


def checksum(path: Path) -> str:
    # Ubuntu 22.04's default Python is 3.10, which has no hashlib.file_digest.
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download_model(root: Path, source: str):
    directory = root / "models"
    directory.mkdir(exist_ok=True)
    target = directory / MODEL_FILE
    if target.exists():
        if checksum(target) != MODEL_SHA256:
            raise ValueError(f"已有模型校验失败：{target}；请移走后重新下载。")
        print("模型 SHA256 校验通过。")
        return
    partial = directory / f"{MODEL_FILE}.part"
    print("下载固定版本 Qwen3-4B Q4_K_M，约 2.5GB；支持断点续传。", flush=True)
    subprocess.run(["curl", "--fail", "--location", "--retry", "3", "--continue-at", "-",
                    "--output", str(partial), MODELSCOPE_URL if source == "modelscope" else MODEL_URL], check=True)
    if checksum(partial) != MODEL_SHA256:
        raise ValueError(f"模型校验失败，保留了 {partial}。请移走该文件后换下载源重试。")
    partial.replace(target)
    print("模型已下载，SHA256 校验通过。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=["huggingface", "modelscope"], default="huggingface")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--configure-only", action="store_true")
    mode.add_argument("--download-only", action="store_true", help="Only download GGUF; leave all configuration unchanged")
    args = parser.parse_args()
    if not args.download_only:
        prepare_env(ROOT)
    if not args.configure_only:
        download_model(ROOT, args.source)


if __name__ == "__main__":
    main()
