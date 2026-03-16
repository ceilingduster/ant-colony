"""DNA parser — loads and validates SKILLS.md into structured data."""

import os
import re
from dataclasses import dataclass, field


@dataclass
class Skill:
    name: str
    description: str


@dataclass
class Guardian:
    name: str
    role: str
    capabilities: list[str]
    restrictions: list[str] = field(default_factory=list)


@dataclass
class DNA:
    identity: str
    environment: str
    guardians: list[Guardian]
    skills: list[Skill]
    health_rules: str
    project_guidelines: str
    failure_handling: str
    scientific_method: str
    first_objective: str
    final_directive: str
    raw: str


def load_dna(path: str | None = None) -> DNA:
    """Load SKILLS.md from disk and parse into structured DNA."""
    if path is None:
        path = os.path.join(os.path.dirname(__file__), "SKILLS.md")

    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()

    return parse_dna(raw)


def parse_dna(raw: str) -> DNA:
    """Parse raw SKILLS.md text into a DNA object."""
    sections = _split_sections(raw)

    guardians = []
    skills = []

    for title, body in sections.items():
        lower = title.lower()
        if "queen" in lower:
            guardians.append(_parse_guardian("Queen", "Filesystem Guardian", body))
        elif "antking" in lower:
            guardians.append(_parse_guardian("Antking", "Command Guardian", body))
        elif "wiseoldant" in lower:
            guardians.append(_parse_guardian("Wiseoldant", "Runtime Guardian", body))
        elif "nurse" in lower:
            guardians.append(_parse_guardian("Nurse", "HTTP Observation Guardian", body))
        elif lower.startswith("skill:"):
            skill_name = title.split(":", 1)[1].strip()
            skills.append(Skill(name=skill_name, description=body.strip()))

    return DNA(
        identity=sections.get("Identity", ""),
        environment=sections.get("Environment Awareness", ""),
        guardians=guardians,
        skills=skills,
        health_rules=sections.get("Health Awareness", ""),
        project_guidelines=sections.get("Project Guidelines", ""),
        failure_handling=sections.get("Failure Handling", ""),
        scientific_method=sections.get("Scientific Method", ""),
        first_objective=sections.get("First Objective", ""),
        final_directive=sections.get("Final Directive", ""),
        raw=raw,
    )


def _split_sections(text: str) -> dict[str, str]:
    """Split markdown into sections by headings (# or ##)."""
    pattern = re.compile(r"^#{1,2}\s+(.+)$", re.MULTILINE)
    matches = list(pattern.finditer(text))
    sections: dict[str, str] = {}
    for i, m in enumerate(matches):
        title = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections[title] = text[start:end].strip()
    return sections


def _parse_guardian(name: str, role: str, body: str) -> Guardian:
    """Extract capabilities list from guardian section body."""
    caps = []
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("*"):
            caps.append(line.lstrip("* ").strip())
    return Guardian(name=name, role=role, capabilities=caps)


def validate_dna(dna: DNA) -> list[str]:
    """Return a list of validation warnings (empty = valid)."""
    warnings = []
    if not dna.identity:
        warnings.append("Missing Identity section")
    if not dna.guardians:
        warnings.append("No guardians defined")
    if not dna.skills:
        warnings.append("No skills defined")
    required_guardians = {"Queen", "Antking", "Wiseoldant", "Nurse"}
    found = {g.name for g in dna.guardians}
    missing = required_guardians - found
    if missing:
        warnings.append(f"Missing guardians: {', '.join(sorted(missing))}")
    return warnings
