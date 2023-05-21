# Syncat

## Overview

Syncat is a `cat`-like utility which uses Vim to display files with syntax
highlighting.

I.e., you can now `syncat code.py`, or even use it with `less` directly, or
via `$LESSOPEN`, and you will see the file appear with the exact same color
theme as when you edit it with Vim.

It requires no extra Vim configuration, no special Vim plugins, it is written
in Python, works either as a standalone utility or in combination with
the venerable `less` utility, and it creates no intermediate files.

Syncat runs Vim against an in-memory terminal emulator to render a file using
Vim's syntax highlighting rules, then scrapes the result and outputs it
to its standard output.

**Note:** Syncat is meant to be a full, drop-in replacement for `cat`, by
executing it directly when it detects that its output is not a terminal. This
is not supported yet.


## Examples

TBD


## Setup

TBD

## Design

The way Syncat works is to run vim, feed its output to a full-fledged in-memory
emulated terminal provided by python-pyte, then scrape the state of the
emulated terminal [foreground and background colors in every cell], and finally
output all characters to the user's actual terminal.

Design principles:

* Run vim against a huge in-memory, emulated VT102 terminal
  [`$COLUMNS` columns by `MAX_LINES` rows], emulated by python-pyte
* Detect exactly which lines vim has touched on the virtual terminal,
  and only scrape these, so the output looks perfect and there are no
  artifacts from leaking ANSI escape codes directly to the user's terminal.
* Direct vim to consume the input directly, do not touch it at all in Syncat.
* Similarly, output all terminal state directly to standard output, without
  any intermediate output files.
* This way we avoid using any temporary files, even when working with streaming
  data coming in from a pipe, which simplifies our design. Everything stays
  in memory.
* Do not depend on Vim plugins at all, no need to touch vim configuration.
* Process Syncat-specific command-line arguments, then pass all of the
  remaining arguments to vim, verbatim, including the actual file name to
  render. This exposes the full range of vim arguments to the end user, for
  maximum flexibility.
* Support retrieving arguments from the environment via the $SYNCCAT env var.
* Detect if we actually had to truncate the output, because the input
  is longer than `MAX_LINES` [currently 65535 lines], and report it to the
  user.
* Preserve vim's actual exit status, which is critical when we run Syncat
  as part of a shell script.


## Dependencies

Syncat requires recent versions of the following Python packages:

* `pyte`: Syncat has been tested with `python3-pyte`>=`0.8.0-2` on Debian.
   **NOTE:** Any pyte version < `0.8.1` seems to suffer from an issue handling
   bright colors, interpreting them as bold instead.

   Syncat runs successfully but you may notice the discrepancy in handling
   bright vs. bold colors compared to running Vim directly on your terminal.
   Until an updated package lands in Debian [report](https://bugs.debian.org/cgi-bin/bugreport.cgi?bug=1036454), you can install pyte from PyPI, so
   it includes the relevant
   [fix](https://github.com/selectel/pyte/commit/4672869d175cea2f80d124f6153fdcc62b53692b).
   For more context see relevant comments
   [here](https://github.com/kovidgoyal/kitty/issues/135#issuecomment-333373766)
   and
   [here](https://github.com/kovidgoyal/kitty/issues/135#issuecomment-433552630).

* `ansi`: Syncat has only been tested with the latest version from PyPI,
   `0.3.6` as of this writing.
   **NOTE:** It has been tested to fail with the Debian-packaged version,
   [report here](https://bugs.debian.org/cgi-bin/bugreport.cgi?bug=1036455),
   because it doesn't include truecolor support.


## TODO

* Document having a Syncat-specific `.vimrc` via `$SYNCCAT`
* Parse Syncat-specific command-line arguments, support an argument to set
  the file type more easily [convert to `set ft=...` in Vim]


## Acknowledgements

Syncat is inspired by

* [vimcat/vimpager](https://github.com/rkitover/vimpager), and
* [vimkat](https://github.com/nkh/vimkat)

Nadim Ibn Hamouda El Khemir, author of vimkat, had the idea of using a terminal
emulator to scrape Vim output.
