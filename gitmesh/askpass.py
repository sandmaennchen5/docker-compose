#!/usr/bin/env python3
"""Git calls this helper for HTTPS prompts; credentials never enter remote URLs."""
import os
import sys

prompt = sys.argv[1].lower() if len(sys.argv) > 1 else ''
print(os.environ.get('GITMESH_GIT_USERNAME' if prompt.startswith('username') else 'GITMESH_GIT_TOKEN', ''))
