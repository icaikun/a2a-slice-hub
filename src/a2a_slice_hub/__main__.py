import argparse

import uvicorn

from .config import load_app


def main():
    parser = argparse.ArgumentParser(description="Run the local Slice Hub API")
    parser.add_argument("--config", default="examples/hub.yaml")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    uvicorn.run(load_app(args.config), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
