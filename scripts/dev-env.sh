# Dependency-free alternative to direnv. Usage:  source scripts/dev-env.sh
# Loads the gitignored .env into the current shell so git/gh/python pick up
# GH_TOKEN and friends. Safe to run repeatedly.
if [ -f "$(dirname "${BASH_SOURCE[0]:-$0}")/../.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$(dirname "${BASH_SOURCE[0]:-$0}")/../.env"
    set +a
    echo "loaded .env (GH_TOKEN set: $([ -n "${GH_TOKEN:-}" ] && echo yes || echo no))"
else
    echo "no .env found — copy .env.example to .env and fill it in" >&2
fi
