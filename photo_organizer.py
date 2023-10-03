#!/usr/bin/env python
# Copyright (C) 2023 Julian Valentin
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import argparse
from collections.abc import Generator, Mapping, Sequence
import contextlib
import datetime
import glob
import hashlib
import os
import pathlib
import re
import shutil
import ssl
import sys
import tempfile
from typing import Any
import urllib.request
import zipfile

import certifi
import exiftool


def main(string_arguments: Sequence[str] | None = None) -> None:
    """Run main entry point.

    :param string_arguments: String arguments to parse. If ``None``, ``sys.argv[1:]`` is used.
    """
    arguments = parse_arguments(string_arguments)
    old_media_paths = collect_media_paths(arguments.glob_patterns)
    if len(old_media_paths) == 0: raise RuntimeError("No files match the given glob patterns.")
    rename_dict = get_rename_dict(old_media_paths)
    if len(rename_dict) == 0:
        print("No renames to perform, exiting.")
        return
    if arguments.dry_run: return
    if (not arguments.force) and (input("Continue [y/n]? ").lower() != "y"): return
    rename(rename_dict)


def parse_arguments(string_arguments: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse program arguments.

    :param string_arguments: String arguments to parse. If ``None``, ``sys.argv[1:]`` is used.
    """
    argument_parser = argparse.ArgumentParser(
        description="Rename photo and video files according to the timestamp they were taken. The "
        "format for the stems of the new filenames is `YYYY-MM-DD_HH-MM-SS_HASH`, where "
        "`YYYY-MM-DD_HH-MM-SS` is the time in local time when the file was created, and `HASH` is "
        "the first 8 bytes of the SHA256 hash of the contents of the file. When run without any "
        "arguments, all files in the current directory are processed (non-recursively), and the "
        "user is asked for confirmation before renaming any files.",
    )
    argument_parser.add_argument(
        "-f",
        "--force",
        action=argparse.BooleanOptionalAction,
        help="Do not ask for confirmation before renaming.",
    )
    argument_parser.add_argument(
        "-n",
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        help="Do not rename anything, just show what would be done.",
    )
    argument_parser.add_argument(
        "glob_patterns",
        metavar="GLOB_PATTERN",
        nargs="*",
        default=["*"],
        help="Glob patterns of the files to rename. `**` is supported. By default, `*` is used, "
        "i.e., all files in the current working directory (non-recursively) are renamed.",
    )
    return argument_parser.parse_args(string_arguments)


def collect_media_paths(glob_patterns: Sequence[str]) -> list[pathlib.Path]:
    """Collect paths of all media files matching the given glob patterns.

    :glob_patterns: Sequence of glob patterns to match ``**`` is supported.
    :return: List of paths of media files (``.jpg``, ``.mov``, ``.png``) matching the glob patterns.
    The paths are sorted for each glob pattern, but the paths of an earlier glob pattern appear
    earlier in the list than the paths of a later glob pattern.
    """
    media_paths: list[pathlib.Path] = []
    for glob_pattern in glob_patterns:
        media_paths.extend(sorted(
            media_path
            for path_string in glob.glob(glob_pattern, recursive=True)
            if (media_path := pathlib.Path(path_string)).suffix.lower() in {".jpg", ".mov", ".png"}
        ))
    return media_paths


def get_rename_dict(old_media_paths: Sequence[pathlib.Path]) -> dict[pathlib.Path, pathlib.Path]:
    """Process media paths and return dict with mapping to new names.

    :param old_media_paths: Sequence of media paths before renaming.
    :return: Dict from old media paths to renamed media paths.
    """
    with contextlib.ExitStack() as exit_stack:
        if (sys.platform == "win32") and (shutil.which("exiftool.exe") is None):
            exit_stack.enter_context(download_exiftool_on_windows())
        exif_tool_helper = exit_stack.enter_context(exiftool.ExifToolHelper())
        print("Retrieving metadata of {} file{}...".format(
            len(old_media_paths),
            "" if len(old_media_paths) == 1 else "s",
        ))
        metadata = exif_tool_helper.get_metadata(old_media_paths)
    rename_dict = {}
    for old_media_path, metadata_entry in zip(old_media_paths, metadata):
        creation_datetime = get_creation_datetime(metadata_entry)
        if creation_datetime is None: continue
        new_media_path = format_media_path(old_media_path, creation_datetime)
        if old_media_path == new_media_path: continue
        print(f"Would rename `{old_media_path}` -> `{new_media_path}`.")
        rename_dict[old_media_path] = new_media_path
    return rename_dict


def get_creation_datetime(metadata: dict[str, Any]) -> datetime.datetime | None:
    """Get creation datetime of media path by parsing exiftool's output.

    :param metadata: Media metadata of the file as returned by exiftool.
    :return: Creation datetime or ``None``, if it has no such datetime or if the datetime has an
    incorrect format.
    """
    media_path = metadata["SourceFile"]
    assert isinstance(media_path, str)
    datetime_pattern = re.compile(
        r"(?P<year>\d+):(?P<month>\d+):(?P<day>\d+) (?P<hour>\d+):(?P<minute>\d+):(?P<second>\d+)"
        r"((?P<timezone_sign>[+-])(?P<timezone_hour>\d+):(?P<timezone_minute>\d+))?",
        flags=re.ASCII,
    )
    for metadata_key in ["EXIF:DateTimeOriginal", "QuickTime:CreationDate"]:
        if metadata_key in metadata:
            creation_datetime_string = metadata[metadata_key]
            assert isinstance(creation_datetime_string, str)
            break
    else:
        print(
            f"Could not find creation datetime in metadata of {media_path}, skipping.",
            file=sys.stderr,
        )
        return None
    regex_match = datetime_pattern.fullmatch(creation_datetime_string)
    if regex_match is None:
        print(
            f"Could not parse creation datetime `{creation_datetime_string}` of `{media_path}`, "
            "skipping.",
            file=sys.stderr,
        )
        return None
    return datetime.datetime(
        int(regex_match.group("year")),
        int(regex_match.group("month")),
        int(regex_match.group("day")),
        int(regex_match.group("hour")),
        int(regex_match.group("minute")),
        int(regex_match.group("second")),
    )


@contextlib.contextmanager
def download_exiftool_on_windows() -> Generator[pathlib.Path, None, None]:
    """Download ExifTool on Windows and place it in the ``PATH``.

    :return: Generator yielding the path of the ExifTool executable once. The executable is placed
    in a temporary directory, which is cleaned up when the generator resumes. In addition, the
    ``PATH`` is reset to its value before calling this function.
    """
    archive_filename = "exiftool-12.67.zip"
    url = f"https://exiftool.org/{archive_filename}"
    print(
        f"Downloading exiftool from `{url}`... (You can skip this by installing exiftool and "
        "adding its directory to your `PATH`.)"
    )
    with tempfile.TemporaryDirectory() as temporary_directory_string:
        temporary_directory = pathlib.Path(temporary_directory_string)
        archive_path = temporary_directory / archive_filename
        # Work around nasty SSL error "certificate has expired" due to the Let's Encrypt
        # certificate not in my trusted root certificate store.
        with urllib.request.urlopen(
            url,
            context=ssl.create_default_context(cafile=certifi.where()),
        ) as response:
            archive_path.write_bytes(response.read())
        executable_in_archive_filename = "exiftool(-k).exe"
        executable_filename = "exiftool.exe"
        with zipfile.ZipFile(archive_path, "r") as zip_file:
            zip_file.getinfo(executable_in_archive_filename).filename = executable_filename
            zip_file.extract(executable_in_archive_filename, temporary_directory)
        archive_path.unlink()
        original_path = os.environ.get("PATH")
        os.environ["PATH"] = "{}{}{}".format(
            temporary_directory,
            os.pathsep,
            os.environ.get("PATH", ""),
        )
        yield temporary_directory / executable_filename
        if original_path is None:
            del os.environ["PATH"]
        else:
            os.environ["PATH"] = original_path


def format_media_path(
    old_media_path: pathlib.Path,
    creation_datetime: datetime.datetime,
) -> pathlib.Path:
    """Format renamed media path.

    :param old_media_path: Media path before renaming.
    :param creation_datetime: Creation datetime of media.
    :return: Renamed media path. It has the same directory and suffix as ``old_media_path``, but
    ``YYYY-MM-DD_HH-MM-SS_HASH`` as stem, where the first part is the datetime in local time when
    the media file was created, and ``HASH`` is the first 8 bytes of the SHA256 hash of the contents
    of the media file.
    """
    hash_ = hashlib.sha256(old_media_path.read_bytes()).hexdigest()
    return old_media_path.with_name("{}_{}{}".format(
        creation_datetime.replace(tzinfo=None).isoformat(sep="_").replace(":", "-"),
        hash_[:8],
        old_media_path.suffix.lower(),
    ))


def rename(rename_mapping: Mapping[pathlib.Path, pathlib.Path]) -> None:
    """Rename files.

    If a file already exists at a renamed path, the source file is removed. This is because the
    renamed paths include the hash of the file, and therefore two files with the same renamed paths
    can be assumed to be equal.

    :param rename_mapping: Mapping from old media paths to renamed media paths.
    """
    print("Renaming {} file{}...".format(
        len(rename_mapping),
        "" if len(rename_mapping) == 1 else "s",
    ), file=sys.stderr)

    for old_path, new_path in rename_mapping.items():
        if new_path.is_file():
            old_path.unlink()
        else:
            old_path.rename(new_path)


if __name__ == "__main__": main()
