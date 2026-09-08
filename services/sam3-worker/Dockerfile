ARG NGC_TAG=25.10-py3
FROM nvcr.io/nvidia/pytorch:${NGC_TAG}
ARG SAM3_REF=8f0b7f4d4e7eda2ed606ebde6702c93359ad01da
ARG UID=1000
ARG GID=1000
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1 SAM3_COMMIT=${SAM3_REF}
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && git clone https://github.com/facebookresearch/sam3.git /opt/sam3 \
 && cd /opt/sam3 \
 && git checkout ${SAM3_REF} \
 && pip install --no-cache-dir --no-build-isolation -e .
COPY packages/pipeline-core /opt/pipeline-core
RUN pip install --no-cache-dir setuptools==80.9.0 wheel==0.45.1 \
 && pip install --no-cache-dir --no-build-isolation /opt/pipeline-core \
 && python -c "import torch; assert torch.version.cuda, 'PyTorch da imagem nao foi compilado com CUDA'"
WORKDIR /app
COPY services/sam3-worker/sam3_runner/ ./sam3_runner/
COPY services/sam3-worker/entrypoint.py /opt/service/entrypoint.py
RUN mkdir -p /workspace /model-cache \
 && chown ${UID}:${GID} /model-cache
ENV HF_HOME=/model-cache MST_WORKSPACE=/workspace
USER ${UID}:${GID}
ENTRYPOINT ["python", "/opt/service/entrypoint.py"]
CMD ["serve"]
