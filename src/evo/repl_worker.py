"""Persistent Python namespace and stdio RPC protocol, invoked with Python -I."""

import ast
import contextlib
import io
import json
import os
import sys
import traceback
from types import SimpleNamespace


def main():
    protocol_out = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", newline="\n")
    protocol_in = os.fdopen(os.dup(sys.stdin.fileno()), encoding="utf-8")
    with open(os.devnull, "r+") as null:
        os.dup2(null.fileno(), sys.stdout.fileno())
        os.dup2(null.fileno(), sys.stdin.fileno())

    def write(value):
        protocol_out.write(json.dumps(value, default=repr, ensure_ascii=True, allow_nan=False) + "\n")
        protocol_out.flush()

    def read():
        line = protocol_in.readline()
        if not line:
            raise EOFError("Host closed the REPL")
        return json.loads(line)

    def proxy(name):
        def call(*args, **kwargs):
            write({"type": "rpc", "name": name, "args": args, "kwargs": kwargs})
            reply = read()
            if "error" in reply:
                raise RuntimeError(reply["error"])
            return reply["result"]
        return call

    namespace = {"__name__": "__evo_repl__"}
    for name in read()["tools"]:
        group, _, method = name.partition(".")
        if method:
            obj = namespace.setdefault(group, SimpleNamespace())
            setattr(obj, method, proxy(name))
        else:
            namespace[group] = proxy(name)

    while True:
        try:
            code = read()["code"]
        except EOFError:
            return
        out, err = io.StringIO(), io.StringIO()
        result = None
        failed = False
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                tree = ast.parse(code, filename="<evo-repl>", mode="exec")
                tail = tree.body.pop() if tree.body and isinstance(tree.body[-1], ast.Expr) else None
                exec(compile(tree, "<evo-repl>", "exec"), namespace)
                if tail:
                    value = eval(compile(ast.Expression(tail.value), "<evo-repl>", "eval"), namespace)
                    if value is not None:
                        namespace["_"] = value
                        result = repr(value)
            except BaseException:
                failed = True
                traceback.print_exc()
        write({"type": "result", "stdout": out.getvalue(), "stderr": err.getvalue(),
               "value": result, "ok": not failed})


if __name__ == "__main__":
    main()
