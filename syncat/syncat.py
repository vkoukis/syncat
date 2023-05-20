# Copyright © 2023 Vangelis Koukis <vkoukis@gmail.com>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""A cat-like utility which uses Vim to display files with syntax highlighting.

Syncat runs Vim against an in-memory terminal emulator to render a file using
Vim's syntax highlighting rules, then scrapes the result and outputs it
to its standard output.

The way syncat works is to run vim, feed its output to a full-fledged in-memory
emulated terminal provided by python-pyte, then scrape the state of the
emulated terminal [foreground and background colors in every cell], and finally
output all characters to the user's actual terminal.

"""

__version__ = "0.0.1"

import os
import sys
import pty
import pyte
import errno
import fcntl
import struct
import termios


# This can't be more than 65535, because the related field
# in the ioctl to set the PTY window size in the kernel is a short int.
MAX_LINES = 65535


def pty_set_winsize(fd, ws_row, ws_col, ws_xpixel=0, ws_ypixel=0):
    """Set the PTY window size in the kernel.

    Set the PTY window size in the kernel, via ioctl().
    See here for the meaning of TIOCSWINSZ:

        https://man7.org/linux/man-pages/man2/ioctl_tty.2.html

    Note the kernel spec defines fields ws_xpixel, ws_ypixel as unused.

    Based on:

        https://stackoverflow.com/questions/6418678/resize-the-terminal-with-python

    """
    winsize = struct.pack("HHHH", ws_row, ws_col, ws_xpixel, ws_ypixel)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


def terminal_set_size(fd, rows, cols):
    """Set the actual window size on the terminal.

    Emit the right ANSI sequence to set the actual window size on the terminal.
    This will actually cause our virtual, emulated terminal to resize itself,
    and allocate more memory to hold its new contents.

    Based on:

        https://stackoverflow.com/questions/6418678/resize-the-terminal-with-python

    """
    seq = ("\x1b[8;%d;%dt" % (rows, cols)).encode("ascii")
    if os.write(fd, seq) != len(seq):
        msg = "InternalError: write to fd %d returned unexpected %d != %d"
        raise RuntimeError(msg)


def construct_vim_cmdline(ifname):
    """Construct the vim command line."""

    # Test with cat for the time being
    return ["cat", ifname]

    # Run Vim
    cl = ["vim"]
    # Set it in readonly mode, so it uses no swapfile, doesn't touch its input
    cl.extend(["-c", "set readonly"])
    # Hide all visual elements, just leave the actual text.
    # This overrides any settings the user may have in their .vimrc,
    # which is great.
    cl.extend(["-c", "set noshowmode"])
    cl.extend(["-c", "set noruler"])
    cl.extend(["-c", "set laststatus=0"])
    cl.extend(["-c", "set noshowcmd"])
    # Ask vim to actually edit the input file
    cl.extend([ifname])
    # Ask vim to redraw the screen, which clears everything and ensures
    # the only thing shown on the emulated terminal is the syntax highlighted
    # text we want to scrape, then quit.
    cl.extend(["+redraw", "+q"])

    return cl


def _dump_screen(screen):
    """Dump the contents of a [hopefully small] pyte Screen.

    Dump the contents of a pyte Screen, surrounded by asterisks,
    useful for debugging.

    """
    print("\n".join("*" + row + "*" for row in screen.display))


def main():
    # TODO: Implement Syncat-specific command-line arguments,
    #       parse the command line and isolate them from the rest of the
    #       arguments, pass all other arguments to Vim verbatim.
    cmdline = construct_vim_cmdline(sys.argv[1])

    # We actually can't retrieve the size of the current terminal
    # from $ROWS and $COLUMNS, because they may be missing from our
    # environment, here is an explanation why:
    #
    #     https://stackoverflow.com/questions/1780483/lines-and-columns-environmental-variables-lost-in-a-script
    #
    # So, query the kernel for the terminal size directly,
    # and keep the values in memory, before we fork the child
    # and it no longer has access to the terminal.

    # Python 3 provides a handy wrapper for TIOCGWINSZ.
    # If we fail to retrieve the terminal size from STDOUT_FILENO [default],
    # try with STDERR_FILENO. This can happen if we're part of a pipeline,
    # so our stdout no longer points to the terminal.
    #
    # TODO: Fall back to executing cat directly, if stdout is not a TTY,
    #       so we can function as a drop-in replacement. Add an option
    #       to enforce syntax highlighting, e.g., `--color=auto/always/never`,
    #       similarly to GNU ls.
    try:
        cols, rows = os.get_terminal_size()
    except OSError as e:
        if e.errno == errno.ENOTTY:
            # Stdout is not a tty.
            # Try with stderr instead.
            cols, rows = os.get_terminal_size(sys.stderr.fileno())
        else:
            raise

    # Fork a child process, have it use its own PTY
    pid, ptyfd = pty.fork()
    if pid == 0:
        # Child process:
        # Standard input and output are connected to the new PTY

        # Configure the terminal:
        # Set kernel PTY size, and actual terminal size.

        # We already know the values for rows, cols.
        # We want our PTY to have the same number of columns as our original
        # terminal, but a huge number of rows.
        rows = MAX_LINES
        pty_set_winsize(sys.stdout.fileno(), rows, cols)
        terminal_set_size(sys.stdout.fileno(), rows, cols)

        # Configure the environment, and replace ourselves with Vim.
        # Useful for debugging our terminal state:
        # os.execlp("/bin/stty", "/bin/stty", "size")
        sys.stderr.write("About to exec: %s\n" % " ".join(cmdline))
        os.execlp(cmdline[0], *cmdline)
        sys.exit(12)
    elif not pid > 0:
        # This shouldn't have happened, Python should have already
        # thrown an exception.
        raise RuntimeError("Internal Error: pty.fork() returned pid < 0")

    # Parent process:
    # We know the PID of the child,
    # and the file descriptor for the new PTY.

    # Allocate a new in-memory emulated terminal, and initialize it.
    # Use a ByteStream to feed it raw bytes, as retrieved from the kernel.
    screen = pyte.Screen(cols, MAX_LINES)
    stream = pyte.ByteStream(screen)

    # TODO: Test performance with byte arrays / memoryviews?
    # buf = byterray(1000)
    # mv = memoryview(buf)

    # Main loop:
    # Ensure the child is still alive,
    # retrieve the bytes it sends to the PTY,
    # and feed them to the emulated terminal.
    child_alive = True
    pty_eio = False
    while child_alive or not pty_eio:
        if child_alive:
            wpid, wstatus = os.waitpid(pid, os.WNOHANG)
            if wpid > 0:
                if wpid != pid:
                    msg = ("Internal Error: waitpid() returned for unexpected"
                           " PID %d != %d" % (wpid, pid))
                    raise RuntimeError(msg)
                # TODO:
                # Only in Python 3.9:
                # sys.exit(os.waitstatus_to_exitcode(wstatus))
                sys.stderr.write("Child exited. PID: %d, status: %d\n" %
                                 (wpid, wstatus))
                # We know the child is dead,
                # no need to call os.waitpid() for it anymore.
                child_alive = False

        # If someone is still using the slave end of the PTY, retrieve data.
        if not pty_eio:
            try:
                # TODO: Test performance with much bigger buffer sizes
                buf = os.read(ptyfd, 1)
            except OSError as e:
                if e.errno == errno.EIO:
                    # This is expected: When the slave end of a PTY has closed,
                    # reading the master end fails with -EIO.
                    # In this case, our work here is done.
                    pty_eio = True
                else:
                    raise

            # We have received some bytes from the PTY,
            # feed them to the emulated terminal, so it can update its state.
            stream.feed(buf)

    # Our work is done.
    # Dump the final state of the emulated terminal to stdout.
    # TODO: Actually dump the attributes of each character as well.
    # import pdb; pdb.set_trace()
    sys.stdout.write("\n".join(row.rstrip() for row in screen.display))
    return 0


if __name__ == "__main__":
    sys.exit(main())
