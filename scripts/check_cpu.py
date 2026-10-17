"""Local CPU smoke/latency check; does not call cloud LLM providers."""
from __future__ import annotations

import json
from pathlib import Path
import time
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
TEXT = ("这周我把阳台上的花盆搬到了窗边。早上浇水时，发现有一片叶子的边缘发黄，"
        "于是拍了张照片，打算周末去花店问问。下午突然下雨，我又把靠外面的两盆搬了回来。")
TEXT = TEXT * 3  # Exceed the short-text routing threshold so Ollama must be exercised.


def request(url: str, payload=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=None if payload is None else json.dumps(payload).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=1800) as response:
        return json.load(response)


def main():
    # dotenv syntax parsing, never source .env.cpu as executable shell code.
    import re
    config = (ROOT / ".env.cpu").read_text(encoding="utf-8")
    matches = re.findall(r"^[ \t]*API_TOKEN[ \t]*=[ \t]*(.*?)[ \t]*$", config, re.MULTILINE)
    if not matches:
        raise SystemExit(".env.cpu 缺少 API_TOKEN")
    token = matches[-1].strip("\"'")
    health = request("http://127.0.0.1:8091/health")
    started = time.monotonic()
    result = request("http://127.0.0.1:8027/api/scan", {"content": TEXT}, token)
    probability = result.get("probability", {})
    if probability.get("status") != "measured":
        raise SystemExit(f"原文评分未成功：{probability}")
    if result["metrics"]["perplexity"] != probability["perplexity"]:
        raise SystemExit("指标与原文评分结果不一致")
    modes = sorted({chunk.get("judge_mode") for chunk in result.get("chunks", [])})
    if "qwen" not in modes:
        raise SystemExit(f"Ollama 未实际完成判别（可能跳过或失败）：{modes}。查看 Compose 日志。")
    if result.get("review", {}).get("used"):
        raise SystemExit("远端复核已启用；此检查预期只用 CPU 本地服务。")
    print(json.dumps({"scorer_health": health, "total_seconds": round(time.monotonic() - started, 2),
                      "judge_modes": modes, "probability": probability,
                      "risk_score": result["risk_score"], "warnings": result.get("warnings", []),
                      "note": "这是连通性和延迟检查，不代表检测准确率。"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
