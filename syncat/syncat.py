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
import types
import errno
import fcntl
import struct
import termios


# This can't be more than 65535, because the related field
# in the ioctl to set the PTY window size in the kernel is a short int.
#
# TODO: The tallest Vim window is 1000 lines...
#       https://vimhelp.org/options.txt.html#%27lines%27
#       This means we'll have to somehow trigger multiple dumps
#       to work with files longer than 1000 lines.
#       This definitely needs a test.
MAX_LINES = 1000


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
    # return ["cat", ifname]

    # Run Vim
    cl = ["vim"]
    # Set it in readonly mode, so it uses no swapfile, doesn't touch its input
    # also disable all viminfo functionality
    cl.extend(["-R", "-i", "NONE"])
    # Hide all visual elements, just leave the actual text.
    # This overrides any settings the user may have in their .vimrc,
    # which is great.
    cl.extend(["-c", "set noshowmode noruler noshowcmd"])
    # Ask vim to actually edit the input file
    cl.extend([ifname])
    # Ask vim to redraw the screen, which clears everything and ensures
    # the only thing shown on the emulated terminal is the syntax highlighted
    # text we want to scrape.
    cl.extend(["+redraw"])
    # We need a way to know how many lines the final text actually is,
    # so we can only dump the lines we need.
    #
    # Failed approach:
    # Move to the last line in the file, then exit Vim, so we can retrieve the
    # position of the emulated cursor when Vim exits and know exactly how many
    # lines our file was. We can't do this, because Vim will actually move the
    # cursor to the bottom of the window before exiting.
    #
    # Current hack:
    # Move to the last line, then trigger the "set window title" ANSI escape
    # sequence, and use the window title as a side channel to pass information
    # [the current line] to Syncat.
    # Also see how we monkey patch the relevant method in the Screen object
    # to run our own callback whenever Vim attempts to set the window title.
    # TODO: Turn this into a way to work around the 1000-line maximum
    #       window size, see comment at MAX_LINES, above.
    cl.extend(["+"])
    cl.extend(["+silent execute \"!echo -n '\033]0;\".line('.').\"\007'\""])
    # Finally, just exit Vim.
    cl.extend(["+q"])
    # TODO:
    # Here is a hack: At this point Vim has actually rendered everything,
    # and we need to snapshot the contents of the emulated terminal,
    # but the process hasn't terminated yet.
    # cl.extend(["+q"])

    return cl


def _dump_screen(screen):
    """Dump the contents of a [hopefully small] pyte Screen.

    Dump the contents of a pyte Screen, surrounded by asterisks,
    useful for debugging.

    """
    print("\n".join("*" + row + "*" for row in screen.display))


def dump_screen(screen, row_start, row_end):
    """Dump specific screen lines, including their attributes."""
    # TODO: Actually dump the attributes of each character as well.
    sys.stdout.write(("\n".join(row.rstrip()
                      for row in screen.display[row_start:row_end])) + "\n")


def set_window_title_cb(screen, title):
    """Callback to monkey-patch into Screen.set_title()."""

    # We're passing the actual number of lines via the side channel
    try:
        row = int(title)
    except Exception as e:
        msg = ("Internal Error: set_window_title_cb: Unexpected title: %s" %
               title)
        raise RuntimeError(msg) from e

    dump_screen(screen, 0, row)


def pty_fork(child_stdin_fd=None, child_stdout_fd=None, child_stderr_fd=None):
    """A pty.fork() equivalent which allows arbitrary redirection.

    This function is equivalent to pty.fork() but it doesn't redirect the
    child's stdin/stdout/stderr to the PTY's slave unconditionally. Instead, it
    allows arbitrary redirection to any file descriptor that the parent already
    has open.

    """

    # Create a new PTY, retrieve the file descriptors for its two ends
    masterfd, slavefd = pty.openpty()

    # Fork!
    pid = os.fork()
    if pid == 0:
        # We're in the child.
        # Use os.dup2() to redirect stdin/stdout/stderr to the
        # specified file descriptors, or to the slave fd, otherwise.
        os.dup2(slavefd if child_stdin_fd is None else child_stdin_fd, 0)
        os.dup2(slavefd if child_stdout_fd is None else child_stdout_fd, 1)
        os.dup2(slavefd if child_stderr_fd is None else child_stderr_fd, 2)
        os.close(slavefd)

        return pid, slavefd
    elif pid > 0:
        # We're in the parent, just return
        os.close(slavefd)
        return pid, masterfd
    else:
        msg = "Internal Error: os.fork() returned pid = %d\n" % pid
        raise RuntimeError(msg)


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
            # Our stdout is not a tty.
            # Try with stderr instead.
            cols, rows = os.get_terminal_size(sys.stderr.fileno())
        else:
            raise

    # Fork a child process for Vim, have it use its own PTY
    # Redirect the child's stdin to our own, presumably a terminal
    # Redirect the child's stderr to our own, to expose any Vim diagnostics
    # as our own.
    pid, ptyfd = pty_fork(child_stdin_fd=sys.stdin.fileno(),
                          child_stderr_fd=sys.stderr.fileno())
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
        # sys.stderr.write("About to exec: %s\n" % " ".join(cmdline))
        os.execlp(cmdline[0], *cmdline)
        sys.exit(12)
    elif not pid > 0:
        # This shouldn't have happened, Python should have already
        # thrown an exception.
        raise RuntimeError("Internal Error: pty_fork() returned pid < 0")

    # Parent process:
    # We know the PID of the child,
    # and the file descriptor for the new PTY.

    # Allocate a new in-memory emulated terminal, and initialize it.
    # Use a ByteStream to feed it raw bytes, as retrieved from the kernel.
    screen = pyte.Screen(cols, MAX_LINES)
    stream = pyte.ByteStream(screen)

    # Monkey patch the set_title() method, so we can detect
    # whenever Vim attempts to update the window title,
    # and dump the screen.
    # See here for why we're using types.MethodType:
    #
    #      https://tryolabs.com/blog/2013/07/05/run-time-method-patching-python
    #
    screen.set_title = types.MethodType(set_window_title_cb, screen)

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
    # We must have already dumped the rendered file,
    # via the set_window_title_cb() callback, above.
    #
    # import pdb; pdb.set_trace()
    # sys.stderr.write(("Cursor: [%d, %d]\n" %
    #                   (screen.cursor.x, screen.cursor.y)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
