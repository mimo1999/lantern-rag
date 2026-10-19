# Two stages: everything is installed into a virtualenv in `builder`, and only the virtualenv and
# the model weights are copied into the final image. pip's build leftovers and the intermediate
# layers of the heavy installs never ship.

# ---- builder ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# The heavy, rarely changing layers come first so editing application code does not download
# PyTorch and the Docling weights again.
# CPU-only PyTorch: the default wheel bundles CUDA libraries (several GB) this service never uses.
RUN pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu

# Docling and its weights, baked in so a PDF upload never downloads anything at runtime.
# Keep this version range equal to the one in pyproject.toml.
ENV DOCLING_ARTIFACTS_PATH=/opt/docling-models HOME=/tmp
RUN pip install "docling>=2.137.0" \
    && docling-tools models download layout tableformer -o /opt/docling-models

# Slimming that cannot affect runtime: bundled test suites, and PyTorch's C++ headers (only
# needed to compile extensions).
RUN find /opt/venv -type d -name tests -prune -exec rm -rf {} + \
    && rm -rf /opt/venv/lib/python3.12/site-packages/torch/include

WORKDIR /build
COPY pyproject.toml ./
COPY app ./app
RUN pip install .

# ---- final image -----------------------------------------------------------------------
FROM python:3.12-slim

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/docling-models /opt/docling-models
ENV PATH="/opt/venv/bin:$PATH" DOCLING_ARTIFACTS_PATH=/opt/docling-models HOME=/tmp

WORKDIR /service
# Demo seeding (APP_ENV=development only, run by the compose `seed` service) needs these; the
# application itself runs from the installed package in the virtualenv.
COPY scripts ./scripts
COPY evaluation ./evaluation
COPY data ./data
RUN useradd --system --no-create-home --uid 10001 appuser

USER appuser
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"

CMD ["sh", "-c", "python -m app.db.migrate && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
