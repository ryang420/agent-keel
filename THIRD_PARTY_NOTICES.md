# Third-party notices

agent-keel adapts code from the following project. Each adapted file names its source in a
header comment.

## hermes-agent (Nous Research)

https://github.com/NousResearch/hermes-agent

Version v2026.9.24 (commit `f97608f178`). Used in `src/keel/providers/responses.py`,
`src/keel/providers/sigv4.py`, `src/keel/providers/retry.py`, the prompt texts and tool definitions under
`src/keel/prompts/`, `src/keel/skills.py` (index format), `src/keel/prompt.py` (tier order), and the tool behaviour in `src/keel/tools/` (result shapes, truncation).

MIT License

Copyright (c) 2025 Nous Research

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
