# Windows Python 3.13 mkdir(mode=0700) drops the restricted-token SID.
# This test-only launcher preserves the workspace's inherited ACL for new temp dirs.
# No ACL is changed on any existing path, and all test data stays in this workspace.
import os
from pathlib import Path
import sys
root = Path(__file__).resolve().parents[1]
original = os.mkdir

def inherited_mkdir(path, mode=0o777, *, dir_fd=None):
    resolved = Path(path).resolve()
    if os.name == 'nt' and mode == 0o700 and resolved.is_relative_to(root):
        mode = 0o777
    return original(path, mode, dir_fd=dir_fd)

os.mkdir = inherited_mkdir
if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main(sys.argv[1:]))
