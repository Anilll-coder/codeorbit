"""The detection rules `codeorbit audit` runs.

Deliberately regex-over-source rather than AST-matched. Two reasons: a rule
stays one readable line so the table below IS the documentation, and a rule can
see things the graph does not model (a string literal's shape, a keyword
argument's value). The graph's job comes after: it says which symbol a hit
lives in and how much code depends on it, which is what turns a list of hits
into a ranked list of risks.

Every rule carries `why` - what actually goes wrong - because a finding a
reader cannot act on is noise.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

HIGH, MEDIUM, LOW = "high", "medium", "low"


@dataclass(frozen=True)
class Rule:
    id: str
    severity: str
    title: str
    why: str
    pattern: re.Pattern
    langs: tuple[str, ...]          # ("python",), ("javascript",) or both
    skip_tests: bool = False        # don't report inside test files


def _r(pat: str) -> re.Pattern:
    return re.compile(pat)


BOTH = ("python", "javascript")
PY = ("python",)
JS = ("javascript",)

RULES: list[Rule] = [
    # ---------------- code execution ----------------
    Rule("py-eval", HIGH, "eval() on runtime data",
         "Any attacker-influenced string becomes code. Use ast.literal_eval or a parser.",
         _r(r"(?<![\w.])eval\s*\("), PY),
    Rule("py-exec", HIGH, "exec() on runtime data",
         "Same as eval: arbitrary code execution. Restructure so code is not built at runtime.",
         _r(r"(?<![\w.])exec\s*\("), PY),
    Rule("py-os-system", HIGH, "os.system() shell call",
         "Runs through a shell, so input containing ; or | executes extra commands. "
         "Use subprocess.run([...]) with a list.",
         _r(r"os\.system\s*\("), PY),
    Rule("py-shell-true", HIGH, "subprocess with shell=True",
         "Reintroduces shell metacharacter injection. Pass a list of arguments instead.",
         _r(r"shell\s*=\s*True"), PY),
    Rule("js-eval", HIGH, "eval() on runtime data",
         "Arbitrary code execution. Use JSON.parse for data.",
         _r(r"(?<![\w.])eval\s*\("), JS),
    Rule("js-child-exec", HIGH, "child_process.exec()",
         "Runs a shell string. Use execFile/spawn with an argument array.",
         _r(r"child_process\s*\.\s*exec\s*\(|(?<![\w.])exec\s*\(\s*[`\"']"), JS),
    Rule("js-new-function", HIGH, "new Function() from a string",
         "Compiles a string as code, the same risk as eval.",
         _r(r"new\s+Function\s*\("), JS),

    # ---------------- deserialization ----------------
    Rule("py-pickle", HIGH, "pickle load of untrusted data",
         "Unpickling executes arbitrary code by design. Use JSON for anything crossing a trust boundary.",
         _r(r"pickle\s*\.\s*loads?\s*\("), PY),
    Rule("py-yaml-load", HIGH, "yaml.load() without a safe loader",
         "The default loader can construct arbitrary Python objects. Use yaml.safe_load.",
         _r(r"yaml\s*\.\s*load\s*\((?![^)]*Safe)"), PY),

    # ---------------- injection ----------------
    Rule("py-sql-fstring", HIGH, "SQL built by string interpolation",
         "Concatenated SQL is injectable. Pass parameters with ? or %s placeholders instead.",
         _r(r"(?:execute|executemany)\s*\(\s*(?:f[\"']|[\"'][^\"']*[\"']\s*%|[\"'][^\"']*[\"']\s*\+)"), PY),
    Rule("js-sql-template", HIGH, "SQL built by template literal",
         "Interpolated SQL is injectable. Use parameterised queries.",
         _r(r"\.\s*query\s*\(\s*`[^`]*\$\{"), JS),
    Rule("js-innerhtml", MEDIUM, "assignment to innerHTML",
         "Writes unescaped markup, so any user text becomes DOM. Use textContent, or sanitise.",
         _r(r"\.\s*innerHTML\s*="), JS),
    Rule("js-dangerously", MEDIUM, "dangerouslySetInnerHTML",
         "Bypasses React's escaping. Sanitise the HTML before it reaches this prop.",
         _r(r"dangerouslySetInnerHTML"), JS),
    Rule("js-doc-write", MEDIUM, "document.write()",
         "Injects unescaped markup and blocks parsing. Build nodes instead.",
         _r(r"document\s*\.\s*write\s*\("), JS),

    # ---------------- secrets ----------------
    Rule("secret-assign", HIGH, "credential assigned in source",
         "A secret in the repository is in its history forever. Read it from the environment "
         "and rotate this one.",
         _r(r"(?i)\b(?:password|passwd|secret|api_?key|access_?token|auth_?token|private_?key)"
            r"\s*[:=]\s*[\"'][^\"'\s]{8,}[\"']"), BOTH, skip_tests=True),
    Rule("secret-aws", HIGH, "AWS access key id in source",
         "A live AWS key. Revoke it and load credentials from the environment or a profile.",
         _r(r"\bAKIA[0-9A-Z]{16}\b"), BOTH),
    Rule("secret-pem", HIGH, "private key block in source",
         "A private key committed to the repository. Remove it and rotate the key.",
         _r(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"), BOTH),

    # ---------------- weak crypto / transport ----------------
    Rule("py-verify-false", HIGH, "TLS verification disabled",
         "verify=False accepts any certificate, so the connection can be intercepted.",
         _r(r"verify\s*=\s*False"), PY),
    Rule("js-tls-reject", HIGH, "TLS verification disabled",
         "rejectUnauthorized:false accepts any certificate.",
         _r(r"rejectUnauthorized\s*:\s*false"), JS),
    Rule("py-weak-hash", MEDIUM, "MD5/SHA1 used for hashing",
         "Both are broken for security use. Use SHA-256, or bcrypt/argon2 for passwords.",
         _r(r"hashlib\s*\.\s*(?:md5|sha1)\s*\("), PY),
    Rule("py-weak-random", MEDIUM, "random module used for a secret",
         "random is predictable from observed output. Use the secrets module for "
         "tokens, passwords, salts and nonces.",
         _r(r"(?i)\b(?:token|secret|password|passwd|salt|nonce|key|otp)\w*\s*=\s*random\s*\."),
         PY),
    Rule("py-mktemp", MEDIUM, "tempfile.mktemp() race",
         "The name can be claimed between the call and the open. Use NamedTemporaryFile or mkstemp.",
         _r(r"tempfile\s*\.\s*mktemp\s*\("), PY),

    # ---------------- error handling ----------------
    Rule("py-bare-except", MEDIUM, "bare except swallows everything",
         "Catches KeyboardInterrupt and SystemExit too, and hides real failures. "
         "Catch the exception you expect.",
         _r(r"except\s*:"), PY),
    Rule("py-except-pass", MEDIUM, "exception silently discarded",
         "A failure here leaves no trace. Log it, or let it propagate.",
         _r(r"except[^:]*:\s*(?:#.*)?\n\s*pass\b"), PY),
    Rule("py-assert-check", MEDIUM, "assert used as a runtime check",
         "python -O strips asserts, so this validation vanishes in optimised runs. Raise instead.",
         _r(r"^\s*assert\s+"), PY, skip_tests=True),
    Rule("js-empty-catch", MEDIUM, "empty catch block",
         "A failure here leaves no trace. Log it, or rethrow.",
         _r(r"catch\s*\([^)]*\)\s*\{\s*\}"), JS),

    # ---------------- leftovers ----------------
    Rule("py-debugger", HIGH, "debugger breakpoint left in code",
         "Halts the process in production. Remove it.",
         _r(r"(?<![\w.])(?:breakpoint\s*\(|pdb\s*\.\s*set_trace\s*\()"), PY),
    Rule("js-debugger", HIGH, "debugger statement left in code",
         "Halts execution when devtools are open. Remove it.",
         _r(r"(?<![\w.])debugger\s*;?\s*$"), JS),
    Rule("todo", LOW, "unresolved TODO/FIXME",
         "A known gap recorded in a comment. Track it, or fix it.",
         _r(r"(?://|#)\s*(?:TODO|FIXME|XXX|HACK)\b"), BOTH),
]

RULES_BY_ID = {r.id: r for r in RULES}

SEVERITY_ORDER = {HIGH: 0, MEDIUM: 1, LOW: 2}


def rules_for(lang: str) -> list[Rule]:
    return [r for r in RULES if lang in r.langs]


def is_test_path(path: str) -> bool:
    p = path.lower()
    return (
        "test" in p or "spec" in p or "/fixtures/" in p
        or p.startswith("tests/") or "/tests/" in p
    )
