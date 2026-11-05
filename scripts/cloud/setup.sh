#!/bin/bash
# Jarvis's Python environment for Claude Code cloud sessions (Linux containers; PLAN section 4 step 1).
#
# Paste this whole file into the cloud environment's Setup script (claude.ai/code -> the environment ->
# Edit -> Setup script). The environment is then cached with the packages installed, and new sessions
# start with them. In a session whose environment has no setup script, run it once by hand from the repo
# root: bash scripts/cloud/setup.sh (about two minutes).
#
# Builds /opt/jarvis-venv from the pinned list below: the environment the Linux test baseline passed
# with (1593 passed, 9 skipped on 2026-11-05), and puts its bin/ first on PATH for later shells, so
# `python -m pytest -q` from the repo root runs the suite. Nothing is read from the repo, because the
# setup script can run before the repo is cloned. Windows-only packages (pywin32, winotify, winrt-*) are
# left out; the tests that need them skip on Linux. Runs again only when the list changes. Always exits
# 0: a failed install must not stop the session from starting.
# To update the list: install the change into the venv, run the tests, then paste `pip freeze` between
# the PINS lines (test_cloud_setup.py checks that it covers backend/requirements.txt).

VENV=/opt/jarvis-venv
PINS=$(cat <<'PINS'
aiohappyeyeballs==2.7.1
aiohttp==3.14.5
aiosignal==1.4.0
alembic==1.20.1
annotated-doc==0.0.5
annotated-types==0.8.0
anyio==4.16.0
attrs==26.1.0
av==19.0.2
beautifulsoup4==4.15.0
certifi==2026.10.23
cffi==2.1.1
charset-normalizer==3.5.2
click==8.5.0
cloudpickle==3.1.2
comtypes==1.4.17
cryptography==50.0.2
ctranslate2==4.8.5
ddgs==9.16.1
dlinfo==2.0.0
duckduckgo_search==8.1.1
espeakng-loader==0.2.4
fastapi==0.162.0
faster-whisper==1.2.1
filelock==4.2.7
flatbuffers==25.12.19
frozenlist==1.8.0
fsspec==2026.9.0
gguf==0.19.0
greenlet==3.5.6
grpcio==1.85.1
h11==0.16.0
h2==4.4.1
hf-xet==1.8.0
hpack==4.2.0
html2text==2025.4.15
http_ece==1.2.1
httpcore==1.0.9
httpcore2==2.16.0
httptools==0.9.0
httpx==0.28.1
httpx2==2.16.0
huggingface_hub==2.4.0
hyperframe==6.1.0
idna==3.20
iniconfig==2.3.1
joblib==1.6.0
kokoro-onnx==0.6.1
lameenc==1.9.0
lxml==6.1.3
Mako==1.4.3
MarkupSafe==3.0.4
multidict==7.2.1
numpy==2.5.4
nvidia-ml-py==13.615.71
onnxruntime==1.31.1
opentelemetry-api==1.45.2
packaging==26.3
phonemizer==3.6.0
pillow==13.0.0
pluggy==1.7.0
portalocker==4.4.2
primp==2.0.1
propcache==0.5.4
protobuf==7.36.2
psutil==7.2.2
py-vapid==1.9.6
pycaw==20260927
pycparser==3.12
pydantic==2.14.2
pydantic-settings==2.16.2
pydantic_core==2.50.2
Pygments==2.21.0
pynvml==13.0.1
pypdf==6.21.4
pyperclip==1.11.0
pytest==9.2.0
pytest-asyncio==1.4.1.post1
python-dotenv==1.2.4
python-multipart==0.0.33
pywebpush==2.5.0
PyYAML==6.0.3
qdrant-client==1.20.0
reportlab==5.1.0
requests==2.34.2
segno==1.6.6
sounddevice==0.5.6
soundfile==0.14.0
soupsieve==3.0.4
SQLAlchemy==2.0.55
sqlmodel==0.0.38
starlette==1.10.0
tokenizers==0.23.3
tqdm==4.70.1
truststore==0.10.5
typing-inspection==0.4.4
typing_extensions==4.16.0
urllib3==2.8.0
uvicorn==0.54.0
uvloop==0.23.0
watchfiles==1.3.0
websockets==17.4
yarl==1.25.1
PINS
)
HASH=$(printf '%s\n' "$PINS" | sha256sum | cut -c1-64)
on_path() {  # later shells (and Claude Code's, which starts from ~/.bashrc) find this venv's python first
  grep -q 'jarvis-venv/bin' "$HOME/.bashrc" 2>/dev/null \
    || echo 'export PATH="/opt/jarvis-venv/bin:$PATH"  # Jarvis (scripts/cloud/setup.sh)' >> "$HOME/.bashrc"
}
if [ -f "$VENV/.jarvis-pins" ] && [ "$(cat "$VENV/.jarvis-pins")" = "$HASH" ]; then
  on_path
  echo "jarvis: $VENV is up to date"
  exit 0
fi

PY=$(command -v python3.13 || command -v python3)
if [ -f /root/.ccr/ca-bundle.crt ]; then  # the cloud proxy re-signs TLS; point pip and uv at its CA
  export SSL_CERT_FILE=/root/.ccr/ca-bundle.crt PIP_CERT=/root/.ccr/ca-bundle.crt
fi
export UV_NATIVE_TLS=true
LIST=$(mktemp)
printf '%s\n' "$PINS" > "$LIST"
START=$(date +%s)

rm -rf /opt/jarvis-venv
if command -v uv >/dev/null 2>&1 && uv venv -q --seed --python "$PY" "$VENV" \
    && uv pip install -q --python "$VENV/bin/python" -r "$LIST"; then
  how=uv
else
  rm -rf /opt/jarvis-venv
  if "$PY" -m venv "$VENV" && "$VENV/bin/pip" install -q -r "$LIST"; then
    how=pip
  else
    echo "jarvis: installing the Python packages failed (see above); the session starts without them"
    rm -f "$LIST"
    exit 0
  fi
fi
rm -f "$LIST"
echo "$HASH" > "$VENV/.jarvis-pins"
on_path
echo "jarvis: $VENV built with $how in $(($(date +%s) - START)) s"
exit 0
