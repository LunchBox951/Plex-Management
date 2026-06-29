#!/usr/bin/env bash
# init.sh - creates clones of various repositories worth viewing

# Behavior:
#   * In the main checkout: clones from Github.
#   * In a git worktree:    creates symlinks to the main checkout

set -euo pipefail

PROTOTYPE_MANAGER="https://github.com/LunchBox951/Plex-Manager.git"
OMBI="https://github.com/Ombi-app/Ombi.git"
OVERSEERR="https://github.com/sct/overseerr.git"

SONARR="https://github.com/Sonarr/Sonarr.git"
RADARR="https://github.com/Radarr/Radarr.git"

PROWLARR="https://github.com/Prowlarr/Prowlarr.git"
JACKETT="https://github.com/Jackett/Jackett.git"


script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$script_dir"

if ! command -v git >/dev/null 2>&1; then
    echo "error: git is required but not installed" >&2
    exit 1
fi

git_dir_abs="$(cd "$(git rev-parse --git-dir)" && pwd)"
git_common_dir_abs="$(cd "$(git rev-parse --git-common-dir)" && pwd)"

if [[ "$git_dir_abs" != "$git_common_dir_abs" ]]; then
    # Worktree: symlink the main checkout's reference directories rather than
    # re-cloning them. This works with both git-cloned and tarball-extracted
    # sources and avoids duplicating gigabytes of read-only data per worktree.
    main_repo="$(dirname "$git_common_dir_abs")"
    for name in prototype ombi overseerr sonarr radarr prowlarr jackett; do
        src="$main_repo/$name"
        if [[ ! -d "$src" ]]; then
            echo "error: $src missing; run init.sh in the main checkout ($main_repo) first." >&2
            exit 1
        fi
        if [[ -L "$name" || -d "$name" ]]; then
            echo "skip: $name already present"
        else
            ln -s "$src" "$name"
            echo "linked $name -> $src"
        fi
    done
    echo "done."
    exit 0
fi

clone_if_missing() {
    local source="$1"
    local target="$2"
    if [[ -d "$target/.git" ]]; then
        echo "skip: $target already present"
        return
    fi
    echo "cloning $source -> $target"
    git clone "$source" "$target"
}

echo "main checkout detected; cloning from GitHub"
clone_if_missing "$PROTOTYPE_MANAGER" "prototype"
clone_if_missing "$OMBI" "ombi"
clone_if_missing "$OVERSEERR" "overseerr"
clone_if_missing "$SONARR" "sonarr"
clone_if_missing "$RADARR" "radarr"
clone_if_missing "$PROWLARR" "prowlarr"
clone_if_missing "$JACKETT" "jackett"

echo "done."