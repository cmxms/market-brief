# market-brief — notes for coding agents

`README.md` is the operating contract; read it first. It covers the pipeline, the rules,
the storage locations and the exit codes, and it is the copy that stays current.

Three things matter more than anything else here:

- The compose step never posts. It runs with the `web` and `file` toolsets only, so it has
  no terminal, and it receives a filtered environment.
- Never invent a number, and never put a link or citation in the brief.
- A brief that fails validation is not published; the run exits non-zero instead.

Workspace-wide rules — how work is approved, what may be pushed — belong to the operator's
own environment and are not part of this repository. Nothing here assumes you have them.

This repository is public, so everything in it has to make sense to a stranger: no local
paths, no references to documents or systems they cannot see, and a README that gets them
from clone to a working run without guessing.

## Commit attribution

A `Co-Authored-By` trailer is for a **human** who actually contributed to that commit, and nobody else. It is
never used for a model, an assistant, or an agent: GitHub counts such a trailer as a co-author and lists
that account as a contributor on the repository, which credits a stranger for work they did not do. The
author of every commit is the workspace account. Machine involvement, where it matters, belongs in the
commit body's prose. A human's trailer whose contribution is real is encouraged — it is how a collaborator
gets the credit their pull request earned.
