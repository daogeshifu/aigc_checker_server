"""Server-selected provider for both Qwen judgment and expression rewriting."""
import os

from services import bailian_client, ollama_client


DETECTION_PROVIDER = os.getenv("DETECTION_PROVIDER", "ollama").strip().lower()
if DETECTION_PROVIDER not in {"ollama", "bailian"}:
    raise ValueError("DETECTION_PROVIDER 只能是 ollama 或 bailian")


def default_model():
    return bailian_client.BAILIAN_MODEL if DETECTION_PROVIDER == "bailian" else ollama_client.OLLAMA_DEFAULT_MODEL


def rewrite_model():
    return (bailian_client.BAILIAN_REWRITE_MODEL if DETECTION_PROVIDER == "bailian"
            else os.getenv("OLLAMA_REWRITE_MODEL", ollama_client.OLLAMA_DEFAULT_MODEL))


async def start_model_client():
    if DETECTION_PROVIDER == "bailian":
        bailian_client.validate_config()
        await bailian_client.start_bailian_client()
    else:
        await ollama_client.start_ollama_client()


async def close_model_client():
    # Also close unused resources if lifespan is restarted in tests or development.
    await bailian_client.close_bailian_client()
    await ollama_client.close_ollama_client()


async def generate_json(**kwargs):
    client = bailian_client if DETECTION_PROVIDER == "bailian" else ollama_client
    return await client.generate_json(**kwargs)


async def generate_text(**kwargs):
    client = bailian_client if DETECTION_PROVIDER == "bailian" else ollama_client
    return await client.generate_text(**kwargs)
