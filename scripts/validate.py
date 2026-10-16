#!/usr/bin/env python3
"""Validate the distributable skill, reference links and public file hygiene."""
import ast
import json
from pathlib import Path
import re
import sys


def main():
    root = Path(__file__).resolve().parents[1]
    skill = root / "skills/expo-ios-release"
    errors = []
    required = ["SKILL.md", "agents/openai.yaml", "scripts/release.py", "scripts/changelog.py", "scripts/github_release.py",
                "references/configuration.md", "references/execution.md", "references/github-release.md"]
    for name in required:
        if not (skill / name).is_file():
            errors.append("Missing skill resource: " + name)
    text = (skill / "SKILL.md").read_text(encoding="utf-8")
    front = re.match(r"\A---\nname: ([a-z0-9-]+)\ndescription: ([^\n]+)\n---\n", text)
    if not front or front[1] != skill.name or len(front[2]) > 1024:
        errors.append("Invalid skill frontmatter/name/description")
    interface = (skill / "agents/openai.yaml").read_text(encoding="utf-8")
    if "$expo-ios-release" not in interface or "allow_implicit_invocation: true" not in interface:
        errors.append("Skill interface/invocation metadata differs")
    for path in root.rglob("*"):
        if not path.is_file() or ".git" in path.relative_to(root).parts or path.name == ".DS_Store":
            continue
        relative = str(path.relative_to(root))
        if "__pycache__" in path.parts or path.suffix in {".pyc", ".p8", ".p12", ".ipa"}:
            errors.append("Non-distributable file: " + relative)
            continue
        value = path.read_text(encoding="utf-8")
        if path.suffix == ".py":
            ast.parse(value, filename=relative)
        if path.suffix == ".json":
            json.loads(value)
        # Real token/key signatures; fake-service sentinel strings are not credentials.
        if re.search(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9]{30,}", value):
            errors.append("Potential credential material in: " + relative)
        if path.suffix == ".md":
            for target in re.findall(r"\]\(([^)]+)\)", value):
                if re.match(r"[a-z]+://", target) or target.startswith("#"):
                    continue
                destination = (path.parent / target.split("#", 1)[0]).resolve()
                if not destination.is_relative_to(root) or not destination.exists():
                    errors.append(f"Broken or external local reference in {relative}: {target}")
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("Skill package, Python sources, JSON inputs, reference links and public file hygiene verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
