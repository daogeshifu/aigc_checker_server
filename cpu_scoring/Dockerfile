FROM python:3.12-slim
ARG BUILD_JOBS=2
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    CMAKE_ARGS="-DGGML_BLAS=ON -DGGML_BLAS_VENDOR=OpenBLAS -DGGML_NATIVE=OFF -DGGML_CUDA=OFF" \
    CMAKE_BUILD_PARALLEL_LEVEL=${BUILD_JOBS} OPENBLAS_NUM_THREADS=4 OMP_NUM_THREADS=4
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake pkg-config libopenblas-dev && rm -rf /var/lib/apt/lists/*
COPY cpu_scoring/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY cpu_scoring ./cpu_scoring
COPY services/__init__.py services/schemas.py ./services/
HEALTHCHECK --interval=15s --timeout=5s --start-period=180s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8091/health', timeout=4)"
CMD ["uvicorn", "cpu_scoring.app:app", "--host", "0.0.0.0", "--port", "8091", "--workers", "1"]
