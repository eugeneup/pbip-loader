"""Compare definitions returned by Fabric after a publish or a rollback."""
from itertools import groupby
import json
import re

from .project import MODEL_SCHEMA, decode, normalized, verify_part_hashes

DATABASE = "definition/database.tmdl"
MODEL = "definition/model.tmdl"
MODEL_PROPERTIES = "definition.pbism"
DIAGRAM = "diagramLayout.json"
SERVICE_TMDL_MARKER = "annotation __PBI_ServiceStoredInTmdl = reserved"
COMPATIBILITY = re.compile(r"(?m)^\tcompatibilityLevel: (\d+)$")
TABLE_REFERENCE = re.compile(r"ref table (?:'(?:[^']|'')+'|[^\s':=]+)")
FENCE = "`" * 3


def tmdl_text(content):
    return content.decode("utf-8-sig").replace("\r\n", "\n").rstrip()


def compatibility(files):
    if DATABASE in files:
        text = tmdl_text(files[DATABASE])
        if FENCE in text:
            return None
        levels = COMPATIBILITY.findall(text)
        if len(levels) == 1:
            return int(levels[0])
    return None


def model_content(text):
    if FENCE in text:
        return text
    lines = text.split("\n")
    if lines.count(SERVICE_TMDL_MARKER) == 1:
        index = lines.index(SERVICE_TMDL_MARKER)
        del lines[index]
        if 0 < index < len(lines) and not lines[index - 1] and not lines[index]:
            del lines[index]
    # Keep the exact inventory of references. A partial declaration may have children
    # attached to a ref. Sort only the standalone form emitted by Fabric.
    active_ref = False
    for line in lines:
        if not line.strip():
            continue
        if active_ref and line[0].isspace():
            return "\n".join(lines)
        if line.startswith("ref table ") and not TABLE_REFERENCE.fullmatch(line):
            return "\n".join(lines)
        active_ref = line.startswith("ref table ")
    return "\n".join(
        line for is_ref, run in groupby(lines, lambda line: line.startswith("ref table "))
        for line in (sorted(run) if is_ref else run)
    )


def ordered_blocks(text, starts, unordered):
    """Canonicalize runs of sibling objects without modifying their declarations."""
    lines = text.split("\n")
    # Verbatim expressions can contain apparent declarations at any indentation.
    if FENCE in text or any(line.lstrip("\t").startswith("///") for line in lines):
        return text
    indices = [i for i, line in enumerate(lines) if starts(line)]
    if not indices:
        return text
    blocks = ["\n".join(lines[i:j]).rstrip("\n")
              for i, j in zip(indices, [*indices[1:], len(lines)], strict=True)]
    return "\n".join([*lines[:indices[0]], "\n\n".join(
        block for is_unordered, run in groupby(blocks, unordered)
        for block in (sorted(run) if is_unordered else run)
    )])


def model_hashes(files):
    canonical = dict(files)
    if MODEL_PROPERTIES in canonical:
        props = json.loads(canonical[MODEL_PROPERTIES].decode("utf-8-sig"))
        if isinstance(props, dict):
            props.setdefault("$schema", MODEL_SCHEMA)
            canonical[MODEL_PROPERTIES] = json.dumps(props).encode("utf-8")
    for path, content in files.items():
        if path == MODEL:
            text = model_content(tmdl_text(content))
        elif path in ("definition/expressions.tmdl", "definition/relationships.tmdl"):
            keyword = "expression " if path.endswith("expressions.tmdl") else "relationship "
            # Pure top-level sibling declarations. Keep unexpected syntax strict.
            source = tmdl_text(content)
            if any(line and not line.startswith((keyword, "\t")) for line in source.split("\n")):
                continue
            text = ordered_blocks(source, lambda line: line.startswith(keyword), lambda block: True)
        elif path.startswith("definition/tables/") and path.endswith(".tmdl"):
            text = ordered_blocks(tmdl_text(content),
                                  lambda line: line.startswith("\t") and line[1:2].strip() != "",
                                  lambda block: block.startswith("\tcolumn "))
        else:
            continue
        canonical[path] = text.encode("utf-8")
    return normalized(canonical)


def verify_publication(expected, actual, kind, *, retained_level=None):
    left, right = decode(expected), decode(actual)
    strict_left, strict_right = normalized(left), normalized(right)
    if kind != "SemanticModel":
        return {"parts": verify_part_hashes(strict_left, strict_right)}

    left_hashes, right_hashes = model_hashes(left), model_hashes(right)
    adjustments = {}
    if (DIAGRAM in left) != (DIAGRAM in right):
        left_hashes.pop(DIAGRAM, None)
        right_hashes.pop(DIAGRAM, None)
        adjustments[DIAGRAM] = ["optional_part_presence"]

    previous, current = compatibility(left), compatibility(right)
    if previous is not None and current is not None and retained_level == current and current > previous:
        # Only an exact, previously attempted upgrade can be retained in a rollback.
        adjusted = COMPATIBILITY.sub(f"\tcompatibilityLevel: {current}", tmdl_text(left[DATABASE]))
        left_hashes[DATABASE] = normalized({DATABASE: adjusted.encode("utf-8")})[DATABASE]
        adjustments[DATABASE] = [f"compatibility_level_retained:{previous}->{current}"]

    count = verify_part_hashes(left_hashes, right_hashes)
    for path in sorted(strict_left.keys() & strict_right.keys()):
        if strict_left[path] == strict_right[path] or path in adjustments:
            continue
        adjustments[path] = ["collection_order"] if path.endswith(".tmdl") else ["default_schema"]
        if path == MODEL:
            source, result = tmdl_text(left[path]), tmdl_text(right[path])
            changes = []
            if (SERVICE_TMDL_MARKER in source.split("\n")) != (SERVICE_TMDL_MARKER in result.split("\n")):
                changes.append("reserved_storage_marker")
            if ([line for line in source.split("\n") if line.startswith("ref table ")]
                    != [line for line in result.split("\n") if line.startswith("ref table ")]):
                changes.append("table_reference_order")
            adjustments[path] = changes
    return {"parts": count, "adjustments": adjustments}
