#!/usr/bin/env python3
"""Apply a small, declarative patch to a ComfyUI workflow JSON (UI format).

provision.sh runs this after fetching a workflow when
config/patches/<workflow-file>.patch.json exists. Patches are idempotent:
running one twice changes nothing the second time.

Patch format:
{
  "description": "why this patch exists",
  "set_widgets": [
    {"node_type": "AdvPromptEnhancer", "index": 0, "value": "Ollama (URL)",
     "expect": "OpenAI API Connection (URL)", "node_id": 12}
  ],
  "remove_nodes": [{"node_type": "RHHiddenNodes"}]
}

- set_widgets: sets widgets_values[index] on every matching node (top level and
  inside subgraphs). "node_id" narrows the match. If "expect" is given and the
  current value is neither "expect" nor "value", the patch aborts, because the
  graph is not the one the patch was written for.
- remove_nodes: deletes matching nodes and every link that touches them.

Usage: patch-workflow.py <workflow.json> <patch.json>
"""
import json
import os
import stat
import sys
import tempfile


class PatchError(Exception):
    pass


def graphs(wf):
    """Yield (nodes, links, link_style) for the top level and every subgraph."""
    yield wf.get("nodes", []), wf.get("links", []), "list"
    for sg in (wf.get("definitions") or {}).get("subgraphs") or []:
        yield sg.get("nodes", []), sg.get("links", []), "object"


def matches(node, rule):
    if node.get("type") != rule["node_type"]:
        return False
    return "node_id" not in rule or node.get("id") == rule["node_id"]


def set_widgets(wf, rules):
    changes = 0
    for rule in rules:
        hit = False
        for nodes, _links, _style in graphs(wf):
            for node in nodes:
                if not matches(node, rule):
                    continue
                values = node.get("widgets_values")
                idx = rule["index"]
                if not isinstance(values, list) or idx >= len(values):
                    raise PatchError(f"{rule['node_type']} #{node.get('id')}: no widget index {idx}")
                hit = True
                current = values[idx]
                if current == rule["value"]:
                    continue
                if "expect" in rule and current != rule["expect"]:
                    raise PatchError(
                        f"{rule['node_type']} #{node.get('id')} widget {idx} is {current!r}, "
                        f"expected {rule['expect']!r} — the graph changed, patch not applied")
                values[idx] = rule["value"]
                changes += 1
                print(f"  set {rule['node_type']} #{node.get('id')} widget[{idx}]: {current!r} -> {rule['value']!r}")
        if not hit:
            raise PatchError(f"no node matched {rule}")
    return changes


def remove_nodes(wf, rules):
    changes = 0
    for nodes, links, style in graphs(wf):
        doomed = {n.get("id") for n in nodes if any(matches(n, r) for r in rules)}
        if not doomed:
            continue
        if style == "list":  # [id, origin_id, origin_slot, target_id, target_slot, type]
            dead = {l[0] for l in links if l[1] in doomed or l[3] in doomed}
            links[:] = [l for l in links if l[0] not in dead]
        else:
            dead = {l.get("id") for l in links if l.get("origin_id") in doomed or l.get("target_id") in doomed}
            links[:] = [l for l in links if l.get("id") not in dead]
        for node in nodes:
            for inp in node.get("inputs") or []:
                if inp.get("link") in dead:
                    inp["link"] = None
            for out in node.get("outputs") or []:
                if out.get("links"):
                    out["links"] = [x for x in out["links"] if x not in dead]
        before = len(nodes)
        nodes[:] = [n for n in nodes if n.get("id") not in doomed]
        changes += before - len(nodes)
        print(f"  removed node(s) {sorted(doomed)} and {len(dead)} link(s)")
    return changes


def main(path, patch_path):
    with open(path, encoding="utf-8") as fh:
        raw = fh.read()
    wf = json.loads(raw)
    with open(patch_path, encoding="utf-8") as fh:
        patch = json.load(fh)
    if "nodes" not in wf:
        raise PatchError("not a UI-format workflow (no 'nodes')")
    changes = remove_nodes(wf, patch.get("remove_nodes", []))
    changes += set_widgets(wf, patch.get("set_widgets", []))
    if not changes:
        print("  already patched, nothing to do")
        return
    indent = 2 if raw.lstrip().startswith("{\n") or raw[:3] == "{\n " else None
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(wf, fh, ensure_ascii=False, indent=indent)
            fh.write("\n")
        # mkstemp creates the file 0600; keep the workflow's own permissions instead.
        os.chmod(tmp, stat.S_IMODE(os.stat(path).st_mode))
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    print(f"  {changes} change(s) written")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    try:
        main(sys.argv[1], sys.argv[2])
    except (PatchError, KeyError, ValueError) as exc:
        sys.exit(f"patch-workflow: {exc}")
