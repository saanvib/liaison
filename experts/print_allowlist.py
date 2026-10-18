"""Print the Vendor Agent's approver allowlist (A12): python -m experts.print_allowlist"""
from experts import load_directory


def allowlist_line() -> str:
    return "APPROVER_ALLOWLIST=" + ",".join(e.slack_user_id for e in load_directory())


if __name__ == "__main__":
    print(allowlist_line())
