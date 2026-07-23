#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CYCLONE_SOURCE="$PROJECT_ROOT/.deps/cyclonedds"
CYCLONE_INSTALL="$CYCLONE_SOURCE/install"
CYCLONE_VERSION="0.10.2"
UNITREE_LOCAL="$PROJECT_ROOT/.deps/unitree_sdk2_python"
VENV="$PROJECT_ROOT/.venv"

for command_name in git cmake python3; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "Error: required command '$command_name' is not installed." >&2
        echo "Install the prerequisites listed in the README and try again." >&2
        exit 1
    fi
done

cd "$PROJECT_ROOT"

if [[ ! -x "$VENV/bin/python" ]]; then
    echo "Creating the project virtual environment…"
    python3 -m venv "$VENV"
fi

if [[ ! -d "$CYCLONE_SOURCE/.git" ]]; then
    echo "Cloning CycloneDDS $CYCLONE_VERSION…"
    mkdir -p "$PROJECT_ROOT/.deps"
    git clone --depth 1 --branch "$CYCLONE_VERSION" \
        https://github.com/eclipse-cyclonedds/cyclonedds.git \
        "$CYCLONE_SOURCE"
elif [[ "$(git -C "$CYCLONE_SOURCE" describe --tags --exact-match 2>/dev/null || true)" != "$CYCLONE_VERSION" ]]; then
    if ! git -C "$CYCLONE_SOURCE" diff --quiet ||
        ! git -C "$CYCLONE_SOURCE" diff --cached --quiet; then
        echo "Error: $CYCLONE_SOURCE contains local changes." >&2
        echo "Commit or discard them before installing CycloneDDS $CYCLONE_VERSION." >&2
        exit 1
    fi
    echo "Selecting CycloneDDS $CYCLONE_VERSION…"
    git -C "$CYCLONE_SOURCE" fetch --depth 1 origin "tag" "$CYCLONE_VERSION"
    git -C "$CYCLONE_SOURCE" checkout --detach "$CYCLONE_VERSION"
fi

echo "Removing previous CycloneDDS build artifacts…"
cmake -E remove_directory "$CYCLONE_SOURCE/build"
cmake -E remove_directory "$CYCLONE_INSTALL"

echo "Building CycloneDDS…"
cmake \
    -S "$CYCLONE_SOURCE" \
    -B "$CYCLONE_SOURCE/build" \
    -DCMAKE_INSTALL_PREFIX="$CYCLONE_INSTALL" \
    -DBUILD_EXAMPLES=OFF \
    -DBUILD_TESTING=OFF \
    -DENABLE_TYPELIB=ON
cmake --build "$CYCLONE_SOURCE/build" --target install --parallel

export CYCLONEDDS_HOME="$CYCLONE_INSTALL"

echo "Installing the CycloneDDS Python binding…"
"$VENV/bin/python" -m pip install \
    --force-reinstall \
    --no-cache-dir \
    --no-binary cyclonedds \
    "cyclonedds==0.10.2"

if [[ ! -d "$UNITREE_LOCAL/.git" ]]; then
    echo "Cloning the official Unitree SDK…"
    git clone --depth 1 \
        https://github.com/unitreerobotics/unitree_sdk2_python.git \
        "$UNITREE_LOCAL"
fi

echo "Installing the local Unitree SDK clone in editable mode…"
"$VENV/bin/python" -m pip uninstall -y unitree-sdk2py
"$VENV/bin/python" -m pip install --no-deps -e "$UNITREE_LOCAL"

echo "Installing the application and remaining dependencies…"
"$VENV/bin/python" -m pip uninstall -y openai-voice-conversation-ptpt my-unitree
"$VENV/bin/python" -m pip install "$PROJECT_ROOT"

echo "Validating the installation…"
"$VENV/bin/python" -c \
    "from dataclasses import dataclass; \
import conversation, cv2, cyclonedds, diagnostics, robot; \
from cyclonedds.domain import DomainParticipant; \
from cyclonedds.idl import IdlStruct; \
from cyclonedds.idl.types import sequence, uint8; \
from cyclonedds.topic import Topic; \
from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient; \
TestType = dataclass(type('InstallTestType', (IdlStruct,), {
    '__annotations__': {'data': sequence[uint8]}
})); \
participant = DomainParticipant(); \
Topic(participant, 'my_unitree_install_test', TestType); \
print('Application, OpenCV, CycloneDDS topic, and AudioClient: OK')"

echo
echo "Installation complete."
echo "Activate the environment with: source \"$VENV/bin/activate\""
echo "Commands: conversation, diagnostics, robot"
