"""Codex noninteractive structured-output adapter."""
from .base import parse_json, CLICodingAdapter


class CodexCliAdapter(CLICodingAdapter):
    executable_name = "codex"

    def argv(self, directory, attempt_dir):
        return [str(self.executable), *self.extra_args, "exec", "--json", "--output-schema",
                str(attempt_dir / "schema.json"), "-o", str(attempt_dir / "last_message.json"),
                "-C", str(directory), "-"]

    def parse_output(self, attempt_dir, stdout):
        # Fail closed on malformed event streams as well as malformed final output.
        for line in stdout.splitlines():
            if line.strip():
                parse_json(line)
        return parse_json((attempt_dir / "last_message.json").read_text(encoding="utf-8"))
