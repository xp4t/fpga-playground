# Lightweight RTL synthesis and simulation backend; no bitstream toolchain.
FROM python:3.11-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends yosys iverilog bubblewrap \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 workbench

WORKDIR /app
COPY server.py ./
COPY boards/ ./boards/
COPY sim-core/ ./sim-core/
COPY bitstream-decode/ ./bitstream-decode/
COPY web/ ./web/
COPY artya7.png nexysa7.avif ./
RUN mkdir .work && chown workbench:workbench .work

USER workbench
ENV PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["sh", "-c", "exec python server.py --host 0.0.0.0 --port \"${PORT:-8000}\""]
