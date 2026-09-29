"""Bundled survey plans.

A plan argument on the command line is either a path to a YAML file or the
name of a plan shipped inside the package.  Names are only consulted when
no file of that name exists, so an explicit path always wins and nothing
that worked before changes meaning.

Bundled plans matter for installed users: after `pipx install rf-survey`
there is no repo checkout, so `examples/plan-uhf-dmr.yml` does not exist
on disk.  `survey run uhf-dmr` does.
"""

import os

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML is a hard dependency
    yaml = None

BUNDLED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "data", "plans")

SUFFIXES = (".yml", ".yaml")


class PlanError(Exception):
    pass


def bundled_names():
    """Sorted names of the plans shipped with the package."""
    if not os.path.isdir(BUNDLED_DIR):
        return []
    names = set()
    for entry in os.listdir(BUNDLED_DIR):
        stem, ext = os.path.splitext(entry)
        if ext in SUFFIXES and not entry.startswith("targets-"):
            names.add(stem)
    return sorted(names)


def bundled_path(name):
    """Absolute path of a bundled plan, or None."""
    for suffix in SUFFIXES:
        path = os.path.join(BUNDLED_DIR, name + suffix)
        if os.path.isfile(path):
            return path
    return None


def describe(path):
    """One-line description of a plan file; '' when it has none."""
    if yaml is None:
        return ""
    try:
        with open(path) as fh:
            doc = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError):
        return ""
    if not isinstance(doc, dict):
        return ""
    desc = doc.get("description")
    return str(desc).strip() if desc else ""


def bundled_catalog():
    """[(name, path, description)] for every bundled plan."""
    out = []
    for name in bundled_names():
        path = bundled_path(name)
        if path:
            out.append((name, path, describe(path)))
    return out


def resolve(plan_arg):
    """Map a CLI plan argument to a readable path.

    An existing file path wins.  Otherwise the argument is treated as the
    name of a bundled plan (with or without a .yml suffix).
    """
    if os.path.isfile(plan_arg):
        return plan_arg

    name = plan_arg
    stem, ext = os.path.splitext(plan_arg)
    if ext in SUFFIXES:
        name = stem
    # Tolerate 'examples/plan-uhf-dmr.yml' and 'plan-uhf-dmr', which is what
    # the old docs told people to type.
    candidates = [name, os.path.basename(name)]
    base = os.path.basename(name)
    if base.startswith("plan-"):
        candidates.append(base[len("plan-"):])

    for candidate in candidates:
        path = bundled_path(candidate)
        if path:
            return path

    known = bundled_names()
    if os.sep in plan_arg or plan_arg.endswith(SUFFIXES):
        hint = "no such file"
    else:
        hint = "not a file, and not a bundled plan"
    raise PlanError(
        "plan %r: %s.\n  bundled plans: %s\n  list them with: survey plans"
        % (plan_arg, hint, ", ".join(known) if known else "(none)"))
