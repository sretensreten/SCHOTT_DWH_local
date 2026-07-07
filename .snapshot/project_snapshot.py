import argparse
import csv
import json
import sys
from pathlib import Path

SNAPSHOT_SUFFIX = "_snapshot"
DEFAULT_CONFIG_DIR = Path(".snapshot") / "config"
DEFAULT_OUTPUT_BASE_DIR = Path(".snapshot") / "snapshot"
AGENTS_FILE_NAME = "AGENTS.md"

DEFAULT_EXCLUDED_DIR_NAMES = {
    "snapshot", ".git", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".venv", "venv", "env",
}
DEFAULT_EXCLUDED_FILE_NAMES = {".gitkeep"}

MARKDOWN_ESCAPES_TO_REPAIR = {
    r"\_": "_", r"\*": "*", r"\[": "[", r"\]": "]",
    r"\(": "(", r"\)": ")", r"\{": "{", r"\}": "}",
    r"\#": "#", r"\+": "+", r"\-": "-", r"\.": ".",
    r"\!": "!", r"\|": "|", r"\>": ">",
}


def find_project_root():
    script_path = Path(__file__).resolve()
    if script_path.parent.name == "tools" and script_path.parent.parent.name == ".snapshot":
        return script_path.parent.parent.parent

    for candidate in [Path.cwd().resolve(), script_path.parent.resolve()]:
        for parent in [candidate, *candidate.parents]:
            if (parent / DEFAULT_CONFIG_DIR).is_dir():
                return parent

    raise FileNotFoundError("Could not find project root with .snapshot/config.")


def config_display_name(config_path):
    name = config_path.stem
    return name[:-len("_snapshot")] if name.endswith("_snapshot") else name


def discover_configs(project_root):
    config_dir = project_root / DEFAULT_CONFIG_DIR
    if not config_dir.is_dir():
        return []
    return [(config_display_name(path), path) for path in sorted(config_dir.glob("*.json"))]


def choose_config_interactively(project_root):
    configs = discover_configs(project_root)
    if not configs:
        raise FileNotFoundError(f"No JSON config files found in {project_root / DEFAULT_CONFIG_DIR}")

    print("\nAvailable snapshot configurations:")
    for index, (name, _) in enumerate(configs, start=1):
        print(f"  {index}. {name}")
    print("  q. quit")

    while True:
        choice = input("\nChoose what you want to snapshot: ").strip().lower()
        if choice in {"q", "quit", "exit"}:
            raise KeyboardInterrupt("Snapshot cancelled by user.")
        if choice.isdigit() and 1 <= int(choice) <= len(configs):
            return configs[int(choice) - 1][1]
        matches = [path for name, path in configs if name.lower() == choice]
        if matches:
            return matches[0]
        print("Invalid choice. Enter a number, config name, or q to quit.")


def normalize_extension(extension):
    extension = extension.strip().lower()
    return extension if extension.startswith(".") else f".{extension}"


def repair_common_markdown_escapes(content):
    for escaped, replacement in MARKDOWN_ESCAPES_TO_REPAIR.items():
        content = content.replace(escaped, replacement)
    return content


def load_config(project_root, config_argument):
    if isinstance(config_argument, Path):
        config_path = config_argument if config_argument.is_absolute() else project_root / config_argument
    else:
        arg_path = Path(config_argument)
        if arg_path.suffix.lower() == ".json":
            config_path = arg_path if arg_path.is_absolute() else project_root / arg_path
        else:
            config_dir = project_root / DEFAULT_CONFIG_DIR
            direct_path = config_dir / f"{config_argument}.json"
            snapshot_path = config_dir / f"{config_argument}_snapshot.json"
            config_path = direct_path if direct_path.is_file() else snapshot_path

    if not config_path.is_file():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as config_file:
        config = json.load(config_file)

    config["__config_path"] = str(config_path)
    config["__config_name"] = config_display_name(config_path)
    return config


def should_exclude_path(path, project_root, config):
    try:
        relative_parts = path.relative_to(project_root).parts
    except ValueError:
        return True

    excluded_dirs = DEFAULT_EXCLUDED_DIR_NAMES | set(config.get("exclude_dirs", []))
    excluded_files = DEFAULT_EXCLUDED_FILE_NAMES | set(config.get("exclude_files", []))

    return any(part in excluded_dirs for part in relative_parts) or path.name in excluded_files


def read_text_file(file_path):
    try:
        content = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    return repair_common_markdown_escapes(content)


def read_csv_preview(file_path, max_rows):
    lines = []
    with open(file_path, "r", encoding="utf-8", errors="replace", newline="") as csv_file:
        reader = csv.reader(csv_file)
        for index, row in enumerate(reader):
            if index > max_rows:
                break
            lines.append(",".join(row))
    return "\n".join([
        f"-- CSV preview only: first {max_rows} data rows plus header when present.",
        f"-- Source file: {file_path.name}",
        "",
        *lines,
    ])


def read_file_for_snapshot(file_path, group_config):
    if file_path.suffix.lower() == ".csv" and group_config.get("csv_preview_rows") is not None:
        return read_csv_preview(file_path, int(group_config["csv_preview_rows"]))
    return read_text_file(file_path)


def clean_previous_snapshots(target_dir):
    if not target_dir.is_dir():
        return
    for file_path in target_dir.glob("*.txt"):
        if SNAPSHOT_SUFFIX in file_path.stem or file_path.name.endswith("full_snapshot.txt"):
            file_path.unlink()


def collect_files_from_dir(project_root, dir_name, extensions, group_config, config):
    start_dir = project_root / dir_name
    if not start_dir.is_dir():
        return []

    candidates = start_dir.iterdir() if group_config.get("exclude_subdirs", False) else start_dir.rglob("*")
    files = []
    for file_path in candidates:
        if not file_path.is_file():
            continue
        if should_exclude_path(file_path, project_root, config):
            continue
        if extensions and file_path.suffix.lower() not in extensions:
            continue
        files.append(file_path)
    return files


def collect_group_files(project_root, group_config, config):
    processed = set()
    files = []
    extensions = {normalize_extension(ext) for ext in group_config.get("extensions", group_config.get("exts", []))}

    for dir_name in group_config.get("dirs", []):
        for file_path in collect_files_from_dir(project_root, dir_name, extensions, group_config, config):
            resolved = file_path.resolve()
            if resolved not in processed:
                files.append(file_path)
                processed.add(resolved)

    for standalone_file in group_config.get("standalone_files", []):
        file_path = project_root / standalone_file
        if file_path.is_file() and not should_exclude_path(file_path, project_root, config):
            resolved = file_path.resolve()
            if resolved not in processed:
                files.append(file_path)
                processed.add(resolved)

    return sorted(files, key=lambda path: path.relative_to(project_root).as_posix())


def get_agent_files(project_root, config):
    return sorted(
        [path for path in project_root.rglob(AGENTS_FILE_NAME) if path.is_file() and not should_exclude_path(path, project_root, config)],
        key=lambda path: path.relative_to(project_root).as_posix(),
    )


def append_file_to_snapshot(project_root, file_path, group_config, toc_entries, snapshot_contents):
    display_path = file_path.relative_to(project_root).as_posix()
    toc_entries.append(display_path)
    snapshot_contents.append(
        f"\n\n## FILE: {display_path}\n"
        f"```{file_path.suffix.lstrip('.')}\n"
        f"{read_file_for_snapshot(file_path, group_config)}\n"
        f"```\n"
    )


def write_snapshot_file(output_file_path, project_label, group_name, toc_entries, snapshot_contents):
    output_file_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_file_path, "w", encoding="utf-8") as output_file:
        output_file.write(f"# LAYER: {project_label}_{group_name} | files: {len(toc_entries)}\n")
        output_file.write("\n## TABLE OF CONTENTS\n")
        for entry in toc_entries:
            output_file.write(f"- {entry}\n")
        output_file.write("\n## CONTENT\n")
        output_file.write("".join(snapshot_contents))
        output_file.write("\n")


def write_group_snapshot(project_root, target_dir, project_label, group_name, group_config, group_files):
    toc_entries = []
    snapshot_contents = []
    for file_path in group_files:
        append_file_to_snapshot(project_root, file_path, group_config, toc_entries, snapshot_contents)

    if not toc_entries and not group_config.get("write_when_empty", False):
        return None

    output_file_name = group_config.get("output_file_name", f"{project_label}_{group_name}_snapshot.txt")
    output_file_path = target_dir / output_file_name
    write_snapshot_file(output_file_path, project_label, group_name, toc_entries, snapshot_contents)
    return output_file_path


def write_full_project_snapshot(target_dir, full_snapshot_file_name, generated_snapshot_files):
    full_output_file_path = target_dir / full_snapshot_file_name
    with open(full_output_file_path, "w", encoding="utf-8") as output_file:
        output_file.write(f"# FULL PROJECT SNAPSHOT | files: {len(generated_snapshot_files)}\n")
        output_file.write("\n## INCLUDED SNAPSHOT FILES\n")
        for snapshot_file in generated_snapshot_files:
            output_file.write(f"- {snapshot_file.name}\n")
        output_file.write("\n## CONTENT\n")
        for snapshot_file in generated_snapshot_files:
            output_file.write(f"\n\n# SNAPSHOT FILE: {snapshot_file.name}\n")
            output_file.write(snapshot_file.read_text(encoding="utf-8"))
    return full_output_file_path


def consolidate_project_files(config_argument=None, selected_groups=None, include_agents=None, clean=True):
    project_root = find_project_root()

    if not config_argument:
        config_argument = choose_config_interactively(project_root)

    config = load_config(project_root, config_argument)
    project_label = config.get("project_label") or config.get("snapshot_prefix") or config.get("__config_name")
    output_folder = config.get("output_folder", f"snapshot_{project_label}")
    full_snapshot_file_name = config.get("full_snapshot_file_name", f"{project_label}_project_full_snapshot.txt")
    target_dir = project_root / DEFAULT_OUTPUT_BASE_DIR / output_folder
    target_dir.mkdir(parents=True, exist_ok=True)

    all_groups = config.get("groups", {})
    selected_groups = [group.strip() for group in selected_groups if group.strip()] if selected_groups else list(all_groups.keys())

    unknown_groups = [group for group in selected_groups if group not in all_groups]
    if unknown_groups:
        raise ValueError(f"Unknown group(s): {', '.join(unknown_groups)}. Available: {', '.join(all_groups.keys())}")

    if include_agents is None:
        include_agents = config.get("agents", {"enabled": True}).get("enabled", True)

    if clean:
        clean_previous_snapshots(target_dir)

    generated_snapshot_files = []
    for group_name in selected_groups:
        snapshot_file = write_group_snapshot(
            project_root,
            target_dir,
            project_label,
            group_name,
            all_groups[group_name],
            collect_group_files(project_root, all_groups[group_name], config),
        )
        if snapshot_file is not None:
            generated_snapshot_files.append(snapshot_file)

    agents_config = config.get("agents", {"enabled": True})
    if include_agents and agents_config.get("enabled", True):
        agent_files = get_agent_files(project_root, config)
        if agent_files or agents_config.get("write_when_empty", False):
            snapshot_file = write_group_snapshot(
                project_root,
                target_dir,
                project_label,
                "agents",
                {"output_file_name": agents_config.get("output_file_name", f"{project_label}_agents_snapshot.txt")},
                agent_files,
            )
            if snapshot_file is not None:
                generated_snapshot_files.append(snapshot_file)

    full_snapshot_file = write_full_project_snapshot(target_dir, full_snapshot_file_name, generated_snapshot_files)

    print(f"Project root: {project_root}")
    print(f"Config: {config['__config_path']}")
    print(f"Snapshot folder: {target_dir}")
    print(f"Selected groups: {', '.join(selected_groups) if selected_groups else '(none)'}")
    print(f"Agents included: {'yes' if include_agents else 'no'}")
    print("Generated snapshots:")
    for snapshot_file in generated_snapshot_files:
        print(f"- {snapshot_file}")
    print(f"Full snapshot: {full_snapshot_file}")


def split_csv_argument(value):
    return [item.strip() for item in value.split(",") if item.strip()] if value else None


def parse_args():
    parser = argparse.ArgumentParser(description="Create AI-friendly snapshots from project files based on JSON configuration.")
    parser.add_argument("config", nargs="?", help="Config name or path. If omitted, config selection menu is shown.")
    parser.add_argument("-g", "--groups", help="Optional comma-separated groups. If omitted, all groups are snapshotted.")
    parser.add_argument("--no-agents", action="store_true", help="Do not include AGENTS.md snapshot.")
    parser.add_argument("--no-clean", action="store_true", help="Do not delete previous generated snapshot txt files.")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    try:
        consolidate_project_files(
            config_argument=args.config,
            selected_groups=split_csv_argument(args.groups),
            include_agents=False if args.no_agents else None,
            clean=not args.no_clean,
        )
    except KeyboardInterrupt as exc:
        print(str(exc) or "Snapshot cancelled.")
        sys.exit(130)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
