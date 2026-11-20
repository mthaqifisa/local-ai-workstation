# Shared by the hooks in .githooks/ (sourced, not run). Keeps personal data out of this public repo.
# The only identities allowed in commit metadata: the owner's GitHub noreply address and GitHub's own.
LAW_ALLOWED_EMAILS='^(34667065\+mthaqifisa@users\.noreply\.github\.com|noreply@github\.com)$'

law_root() { git rev-parse --show-toplevel; }

law_need_gitleaks() {
    command -v gitleaks >/dev/null 2>&1 && return 0
    printf >&2 'privacy: gitleaks is not installed (brew install gitleaks); refusing to continue.\n'
    exit 1
}

# law_scan_stdin <label>: gitleaks over text on stdin with the repo's privacy rules.
law_scan_stdin() {
    if ! gitleaks stdin --no-banner --redact --log-level error --config "$(law_root)/.gitleaks.toml"; then
        printf >&2 'privacy: %s contains personal data or a secret (see above). Fix it; do not allow-list it.\n' "$1"
        exit 1
    fi
}

# law_check_email <email> <what>: fail unless the address is allowed in commit metadata.
law_check_email() {
    if ! printf '%s\n' "$1" | grep -Eq "$LAW_ALLOWED_EMAILS"; then
        printf >&2 'privacy: %s email is not the GitHub noreply address.\n' "$2"
        printf >&2 '  git config user.name thaqif && git config user.email 34667065+mthaqifisa@users.noreply.github.com\n'
        exit 1
    fi
}
