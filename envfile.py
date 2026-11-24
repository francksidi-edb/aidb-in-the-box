#!/usr/bin/env python3
"""Reads a .env file (KEY=VALUE lines) so AI in the Box and every notebook use the same settings.

  python3 envfile.py .env        prints `export KEY='value'` lines for a shell:  eval "$(python3 envfile.py .env)"
  envfile.apply(path)            sets os.environ from the file (used by box_server.py)

Accepted: comments (# ...), blank lines, `export KEY=value`, 'single' and "double" quotes, and a trailing
` # comment` after an unquoted value. Values are never run through a shell, so characters such as $ & ! # in a
password are safe when the value is quoted. Nothing is expanded or evaluated."""
import os
import re
import shlex
import sys

KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse(path):
    out = {}
    try:
        with open(path, encoding="utf-8-sig") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if not sep or not KEY_RE.match(key):
            continue
        if len(val) >= 2 and val[0] in "\"'" and val.endswith(val[0]):
            q, val = val[0], val[1:-1]
            if q == '"':
                val = val.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")
        else:
            val = re.sub(r"\s+#.*$", "", val)
        out[key] = val
    return out


def apply(path, override=True):
    vals = parse(path)
    for k, v in vals.items():
        if override or k not in os.environ:
            os.environ[k] = v
    return sorted(vals)


if __name__ == "__main__":
    for k, v in parse(sys.argv[1] if len(sys.argv) > 1 else ".env").items():
        print("export %s=%s" % (k, shlex.quote(v)))
