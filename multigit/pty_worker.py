"""Linux PTY child: claim a controlling terminal before executing MultiGit."""
import fcntl
import os
import sys
import termios

fcntl.ioctl(0, termios.TIOCSCTTY, 0)
os.environ['GPG_TTY'] = os.ttyname(0)
os.execvp('multigit', ['multigit', *sys.argv[1:]])
