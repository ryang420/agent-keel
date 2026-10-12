# Runtime environment

Your `terminal`, `execute_code`, `read_file`, `write_file`, `patch`, and `search_files` tools all operate inside a sandbox container — NOT on the machine where the agent service runs. The sandbox's working directory at the start of this conversation is {workdir}. Its current user and $HOME are not listed here; if you need them, probe directly with a terminal call like `whoami && pwd`.
