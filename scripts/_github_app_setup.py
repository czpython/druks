import base64
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ENV_PATH = Path(".env")
MANIFEST_DIR = Path(__file__).parent / "manifests"
SETUP_PAGE = "https://druks.ai/app-setup/"

# (role, env key, pem path, default app name)
ROLES = (
    ("operator", "GITHUB_OPERATOR_APP_ID", Path("secrets/operator.pem"), "druks-operator"),
    ("reviewer", "GITHUB_REVIEWER_APP_ID", Path("secrets/reviewer.pem"), "druks-critic"),
)


def read_env() -> dict[str, str]:
    values = {}
    for line in ENV_PATH.read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key] = value
    return values


def patch_env(updates: dict[str, str]) -> None:
    lines = ENV_PATH.read_text().splitlines()
    pending = dict(updates)
    for i, line in enumerate(lines):
        key = line.partition("=")[0]
        if key in pending:
            lines[i] = f"{key}={pending.pop(key)}"
    lines.extend(f"{key}={value}" for key, value in pending.items())
    ENV_PATH.write_text("\n".join(lines) + "\n")


def create_link(manifest: str, org: str) -> str:
    # GitHub only accepts a manifest via form POST, so the link points at a
    # static public page that forwards it. The manifest rides the URL fragment,
    # which the browser never sends anywhere — not even to that page's host.
    payload = base64.urlsafe_b64encode(manifest.encode()).decode()
    return f"{SETUP_PAGE}#org={org}&manifest={payload}"


def exchange(code: str) -> dict:
    # The operator's browser only ever carries the short-lived ?code=; the
    # conversion that returns the PEM happens here on the install host.
    request = urllib.request.Request(
        f"https://api.github.com/app-manifests/{code}/conversions",
        method="POST",
        headers={"Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(request) as response:
        return json.load(response)


def prompt_code(role: str) -> str:
    raw = input(f"Paste the ?code=... value from the {role} redirect: ").strip()
    # Tolerate pasting the whole redirect URL.
    if "code=" in raw:
        raw = raw.partition("code=")[2].partition("&")[0]
    return raw


def provision(
    role: str,
    env_key: str,
    pem_path: Path,
    default_name: str,
    public_url: str,
    org: str,
) -> None:
    env = read_env()
    if env.get(env_key):
        answer = input(f"{env_key}={env[env_key]} already configured. Re-create? [y/N] ")
        if answer.strip().lower() != "y":
            print(f"→ skipping {role}")
            return

    name = input(f"App name for {role} [{default_name}]: ").strip() or default_name
    manifest = (
        (MANIFEST_DIR / f"{role}.json")
        .read_text()
        .replace("__PUBLIC_URL__", public_url)
        .replace("__NAME__", name)
    )
    # Compact — the manifest rides base64-encoded inside the link.
    manifest = json.dumps(json.loads(manifest), separators=(",", ":"))

    print(f"\nOpen this link and click Create GitHub App:\n\n{create_link(manifest, org)}")
    print(
        "\nAfter you click Create, GitHub redirects to a URL ending in"
        "\n?code=... — copy that code. (The page itself may 404; the code"
        "\nis in the address bar.)\n"
    )

    while True:
        try:
            converted = exchange(prompt_code(role))
            break
        except urllib.error.HTTPError as error:
            print(f"✗ exchange failed ({error.code}) — codes are single-use and expire in ~1h.")
            print("  Re-open the HTML page to mint a fresh code, then paste it again.")

    pem_path.write_text(converted["pem"])
    pem_path.chmod(0o600)
    updates = {env_key: str(converted["id"])}
    if role == "operator":
        # Druks verifies inbound webhooks against this; GitHub generated it
        # during the conversion, so the pre-seeded random value is replaced.
        updates["DRUKS_WEBHOOK_SECRET"] = converted["webhook_secret"]
    patch_env(updates)

    print(f"\n✓ {role} app created: id={converted['id']} slug={converted['slug']}")
    print(f"  PEM → {pem_path}")
    print(f"  Install it on your org/repos: {converted['html_url']}/installations/new\n")


def main() -> int:
    if not ENV_PATH.exists():
        print("No .env here — run install.sh first, then re-run with --apps.")
        return 1

    public_url = read_env().get("DRUKS_PUBLIC_URL") or input(
        "Public base URL druks will be reachable on (e.g. https://druks.example.com): "
    ).strip().rstrip("/")
    org = input("GitHub org slug (empty for a personal account): ").strip()

    for role, env_key, pem_path, base_name in ROLES:
        # App names are globally unique across GitHub; the org prefix gives
        # every deployment its own namespace.
        default_name = f"{org}-{base_name}" if org else base_name
        provision(role, env_key, pem_path, default_name, public_url, org)
    return 0


if __name__ == "__main__":
    sys.exit(main())
