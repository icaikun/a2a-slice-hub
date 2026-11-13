"""Claude print-mode structured-output adapter."""
import json

from ..errors import GateRejected
from .base import parse_json, CLICodingAdapter, CODING_RESULT_SCHEMA


class ClaudeCliAdapter(CLICodingAdapter):
    executable_name = "claude"

    def argv(self, directory, attempt_dir):
        return [str(self.executable), *self.extra_args, "-p", "--output-format", "json",
                "--json-schema", json.dumps(CODING_RESULT_SCHEMA, separators=(",", ":"))]

    def parse_output(self, attempt_dir, stdout):
        output = parse_json(stdout)
        if isinstance(output, dict) and output.get("is_error"):
            raise GateRejected("Claude reported an unsuccessful result")
        return output.get("structured_output", output) if isinstance(output, dict) else output
