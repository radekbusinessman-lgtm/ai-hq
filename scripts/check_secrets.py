"""Kontrola před commitem: zablokuje commit, pokud obsahuje soubor .env nebo něco jako API klíč.

Instalace (jednou):  sh scripts/install_hooks.sh
"""

import re
import subprocess
import sys

PATTERNS = {
    "Anthropic API klíč": re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    "OpenAI API klíč": re.compile(r"sk-(proj-)?[A-Za-z0-9]{32,}"),
    "Discord bot token": re.compile(r"[MNO][A-Za-z\d_-]{23,25}\.[A-Za-z\d_-]{6}\.[A-Za-z\d_-]{27,}"),
    "Privátní klíč": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "Heslo v URL databáze": re.compile(r"postgres(ql)?(\+\w+)?://[^:\s]+:(?!aihq_dev@)[^@\s]+@"),
}


def staged_files() -> list[str]:
    out = subprocess.run(  # noqa: S603, S607
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        capture_output=True, text=True, check=True,
    )
    return [f for f in out.stdout.splitlines() if f]


def main() -> int:
    problems: list[str] = []
    for path in staged_files():
        if re.search(r"(^|/)\.env($|\.)", path) and not path.endswith(".env.example"):
            problems.append(f"{path}: soubor .env se nesmí commitovat")
            continue
        content = subprocess.run(  # noqa: S603, S607
            ["git", "show", f":{path}"], capture_output=True, text=True, errors="ignore"
        ).stdout
        for label, pattern in PATTERNS.items():
            if pattern.search(content):
                problems.append(f"{path}: vypadá to na {label}")
    if problems:
        print("COMMIT ZASTAVEN – možný únik tajemství:\n  " + "\n  ".join(problems))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
