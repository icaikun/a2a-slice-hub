"""Legacy API invocation plus explicit serve and offline skills verification."""
import argparse
import json
import sys

import uvicorn

from .config import load_app


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # Preserve the old option-only invocation exactly.
    if not argv or argv[0] not in {"serve", "verify-skills", "-h", "--help"}:
        argv.insert(0, "serve")
    parser = argparse.ArgumentParser(description="Slice Hub API and governance tools")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Run the local Slice Hub API")
    serve.add_argument("--config", default="examples/hub.yaml")
    serve.add_argument("--host", default="localhost")
    serve.add_argument("--port", type=int, default=8000)
    verify = commands.add_parser("verify-skills", help="Verify committed skills and Claude mirrors")
    verify.add_argument("--repo", required=True)
    verify.add_argument("--rev", required=True)
    args = parser.parse_args(argv)
    if args.command == "serve":
        uvicorn.run(load_app(args.config), host=args.host, port=args.port)
    else:
        from pathlib import Path
        from .context import verify_skills_lock
        from .errors import Conflict, GateRejected, InfrastructureError
        from .git import Git
        try:
            # Verification creates no worktree or scratch directory in the repository.
            git = Git(args.repo, Path(args.repo))
            rev = git.text("rev-parse", "--verify", "--end-of-options", args.rev + "^{commit}")
            manifest, digest = verify_skills_lock(git, rev)
        except (Conflict, GateRejected, InfrastructureError) as exc:
            parser.exit(1, type(exc).__name__ + ": skills verification failed\n")
        print(json.dumps({"revision": rev, "skills": manifest, "digest": digest}, sort_keys=True))


if __name__ == "__main__":
    main()
