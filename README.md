<!--
   - Copyright (C) 2023 Julian Valentin
   -
   - This Source Code Form is subject to the terms of the Mozilla Public
   - License, v. 2.0. If a copy of the MPL was not distributed with this
   - file, You can obtain one at https://mozilla.org/MPL/2.0/.
   -->

# photo_organizer

Rename photo and video files according to the timestamp they were taken. The format for the stems of the new filenames is `YYYY-MM-DD_HH-MM-SS_HASH`, where `YYYY-MM-DD_HH-MM-SS` is the time in local time when the file was created, and `HASH` is the first 8 bytes of the SHA256 hash of the contents of the file. When run without any arguments, all files in the current directory are processed (non-recursively), and the user is asked for confirmation before renaming any files.
