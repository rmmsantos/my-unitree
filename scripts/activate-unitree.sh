#!/usr/bin/env bash

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    echo "Source this script instead of executing it:" >&2
    echo "  source scripts/activate-unitree.sh" >&2
    exit 1
fi

UNITREE_PROJECT_ROOT="$(
    cd "$(dirname "${BASH_SOURCE[0]}")/.." >/dev/null 2>&1 && pwd
)"

source "$UNITREE_PROJECT_ROOT/.venv/bin/activate"

export CYCLONEDDS_HOME="$UNITREE_PROJECT_ROOT/.deps/cyclonedds/install"
case ":${LD_LIBRARY_PATH:-}:" in
    *":$CYCLONEDDS_HOME/lib:"*) ;;
    *) export LD_LIBRARY_PATH="$CYCLONEDDS_HOME/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" ;;
esac

unset UNITREE_PROJECT_ROOT
