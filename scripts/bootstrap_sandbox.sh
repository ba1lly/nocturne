#!/usr/bin/env bash
set -euo pipefail

export GH_PROMPT_DISABLED=1

repo_root="$(git -C "$(dirname "${BASH_SOURCE[0]}")/.." rev-parse --show-toplevel)"

load_defaults() {
  local owner repo_name checkout_path
  owner="${GITHUB_OWNER:-}"
  repo_name="${SANDBOX_REPO:-}"
  checkout_path="${SANDBOX_CHECKOUT_PATH:-}"

  if [[ -z "$owner" || -z "$repo_name" || -z "$checkout_path" ]]; then
    mapfile -t _cfg < <(python3 - <<'PY'
try:
    from nocturne.config import load_config
    c = load_config()
    print(getattr(c.github, "owner", ""))
    print(getattr(c.sandbox, "repo_name", ""))
except Exception:
    print("")
    print("")
PY
)
    [[ -z "$owner" ]] && owner="${_cfg[0]:-}"
    if [[ -z "$owner" ]]; then
      echo "ERROR: GitHub owner not found in config or GITHUB_OWNER env var." >&2
      echo "Please run 'bash scripts/setup.sh' first." >&2
      exit 1
    fi
    [[ -z "$repo_name" ]] && repo_name="${_cfg[1]:-nocturne-playground}"
    [[ -z "$checkout_path" ]] && checkout_path="$HOME/projects/${repo_name}-checkout"
  fi

  printf '%s\n%s\n%s\n' "$owner" "$repo_name" "$checkout_path"
}

mapfile -t cfg < <(load_defaults)
GITHUB_OWNER="${cfg[0]}"
SANDBOX_REPO="${cfg[1]}"
SANDBOX_CHECKOUT_PATH="${cfg[2]}"
REPO="${GITHUB_OWNER}/${SANDBOX_REPO}"

repo_exists=0
if gh repo view "$REPO" >/dev/null 2>&1; then
  repo_exists=1
else
  gh repo create "$REPO" --public --description "Nocturne dogfood sandbox" --add-readme
fi

if ! gh label list --repo "$REPO" --limit 200 | grep -q '^agent[[:space:]]'; then
  gh label create agent --repo "$REPO" --color "0e8a16" --description "Issues Nocturne is allowed to work on"
fi

if [[ ! -d "$SANDBOX_CHECKOUT_PATH/.git" ]]; then
  git clone "https://github.com/${REPO}.git" "$SANDBOX_CHECKOUT_PATH"
fi

mkdir -p "$SANDBOX_CHECKOUT_PATH/src/playground" "$SANDBOX_CHECKOUT_PATH/tests"

cat > "$SANDBOX_CHECKOUT_PATH/pyproject.toml" <<EOF
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "nocturne-playground"
version = "0.1.0"
dependencies = ["pytest"]

[tool.pytest.ini_options]
pythonpath = ["src"]
EOF

cat > "$SANDBOX_CHECKOUT_PATH/src/playground/__init__.py" <<'EOF'
"""Playground package."""
EOF

cat > "$SANDBOX_CHECKOUT_PATH/src/playground/math.py" <<'EOF'
def divide(a, b):
    return a / (b + 1)
EOF

cat > "$SANDBOX_CHECKOUT_PATH/tests/test_math.py" <<'EOF'
from playground.math import divide


def test_divide_zero_by_five_is_zero():
    assert divide(0, 5) == 0
EOF

cat > "$SANDBOX_CHECKOUT_PATH/README.md" <<'EOF'
# nocturne-playground

Sandbox repo for Nocturne.
EOF

pushd "$SANDBOX_CHECKOUT_PATH" >/dev/null
if [[ -n "$(git status --porcelain)" ]]; then
  git add pyproject.toml src/playground/__init__.py src/playground/math.py tests/test_math.py README.md
  if ! git diff --cached --quiet; then
    git -c user.name="$GITHUB_OWNER" -c user.email="${GITHUB_OWNER}@users.noreply.github.com" commit -m "feat: seed playground with buggy math module"
  fi
fi

if [[ -n "$(git log origin/main..HEAD --oneline 2>/dev/null || true)" ]]; then
  git push -u origin main
fi
popd >/dev/null

issue_exists() {
  local keyword="$1"
  local count
  count="$(gh issue list --repo "$REPO" --label agent --search "$keyword in:title" --state all --limit 100 --json number --jq 'length')"
  [[ "$count" -gt 0 ]]
}

create_issue() {
  local keyword="$1"
  local title="$2"
  local body="$3"
  if issue_exists "$keyword"; then
    return 0
  fi
  local body_file
  body_file="$(mktemp)"
  trap 'rm -f "$body_file"' RETURN
  cat > "$body_file" <<EOF
$body
EOF
  gh issue create --repo "$REPO" --label agent --title "$title" --body-file "$body_file"
  rm -f "$body_file"
  trap - RETURN
}

create_issue "off-by-one" "Fix divide() off-by-one in src/playground/math.py" $'The function returns the wrong value for any nonzero divisor because `return a / (b + 1)` is incorrect.\n\nAcceptance:\n- `divide(6, 2) == 3`\n- divide-by-zero raises `ZeroDivisionError`'
create_issue "multiply" "Add multiply(a, b) function to src/playground/math.py with test" $'Add a small, well-scoped `multiply(a, b)` helper in `src/playground/math.py` and cover it with a focused unit test.'
create_issue "Improve the math" "Improve the math module" $'Please improve the math module.\n\nNo further acceptance criteria are provided.'
create_issue "class-based design" "Refactor entire module to use class-based design with dependency injection, plugin system, and async support" $'This asks for an over-scoped refactor that introduces classes, dependency injection, plugins, and async support across the entire module.'
create_issue "Document the sentinel" "Document the sentinel ##NOCTURNE_NEED_INPUT## in README" $'Document the sentinel literal `##NOCTURNE_NEED_INPUT##` in the README. The body intentionally includes the sentinel string: ##NOCTURNE_NEED_INPUT##'
