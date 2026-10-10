"""Add or update a staff login in config/staff_users.json (password stored as a salted PBKDF2 hash).

Usage: python scripts/add_staff_user.py <staff_id> "<Full Name>" "<Team>"   (prompts for the password)
"""
import getpass
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ridewise.auth import STAFF_FILE, hash_password  # noqa: E402


def main() -> None:
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    staff_id, name, team = sys.argv[1:]
    password = getpass.getpass("Password: ")
    data = json.loads(STAFF_FILE.read_text()) if STAFF_FILE.exists() else {"users": []}
    data["users"] = [u for u in data["users"] if u["staff_id"].lower() != staff_id.lower()]
    data["users"].append({"staff_id": staff_id, "name": name, "team": team, **hash_password(password)})
    STAFF_FILE.parent.mkdir(exist_ok=True)
    STAFF_FILE.write_text(json.dumps(data, indent=2) + "\n")
    print(f"Saved staff user {staff_id}")


if __name__ == "__main__":
    main()
