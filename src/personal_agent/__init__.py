RESTART_EXIT_CODE = 75


def main() -> None:
    """Supervisor: runs the server in a child process and starts it again when it asks to restart."""
    import os
    import subprocess
    import sys

    env = {**os.environ, "PA_SUPERVISED": "1"}
    while True:
        try:
            code = subprocess.call([sys.executable, "-m", "personal_agent.serve"], env=env)
        except KeyboardInterrupt:
            return
        if code != RESTART_EXIT_CODE:
            sys.exit(code)
        print("personal-agent: restarting", flush=True)
