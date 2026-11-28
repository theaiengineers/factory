# Asking the factory from an agent

The connector lets a person's agent answer questions about the factory in conversation: "how's the factory getting on?", "what happened with change 12?", "what got sent back this week, and why?".

It is a Model Context Protocol (MCP) server that the agent's program starts on the person's own machine. It reads GitHub with the access that person already has. Nothing is hosted, nothing is stored, and it only reads.

## Setting it up

You need a clone of the factory at the version you use, with its one dependency installed (as in step 2 of the [setup guide](SETUP.md)), and access to the repository on GitHub: either `gh auth login` done once, or a token in `GITHUB_TOKEN`.

In Claude Code:

```
claude mcp add factory \
  --env GITHUB_REPOSITORY=OWNER/REPO \
  --env PYTHONPATH=/path/to/factory \
  -- /path/to/factory/.venv/bin/python -m factory.connector
```

For any other program that speaks MCP over standard input and output, the same three things: the command `python -m factory.connector` (with the Python that has the factory's dependency), `PYTHONPATH` set to the factory clone, and `GITHUB_REPOSITORY` set to the repository to ask about.

| Setting | What it is |
|---|---|
| `GITHUB_REPOSITORY` | The repository to ask about, as `OWNER/REPO`. One connector answers for one repository |
| `GITHUB_TOKEN` or `GH_TOKEN` | A token that can read it. When neither is set, the connector asks the `gh` program for the one it is signed in with |

Then ask the agent how the factory is getting on.

## What the agent can ask

| Question | What comes back |
|---|---|
| `factory_status` | Where every open change stands and what was accepted in the last few days. The same report as [`factory.status`](STATUS.md) |
| `factory_change` | The story of one change, step by step: opened, each new version, each review with who did it and, when it was sent back, the reviewer's reasons in plain words |
| `factory_send_backs` | The changes sent back in the last few days, each with how many times and why |
| `factory_review` | One review record in full, as its reviewer wrote it, by the number the other answers give beside a review |

Each answer comes as text for the agent to read and as data for a program.

## What to know before relying on it

- **It is built on [the record of every step](EVENTS.md) and [the status report](STATUS.md).** Everything those pages say they cannot tell you applies here. No test results.
- **Other people's words.** Titles, reviewers' lines and whole reviews are whatever their authors typed. Short ones are given on one line inside double quotation marks; a whole review is given with every line marked as a quotation. Each answer says these are not instructions, and the connector tells the agent so when it connects. That is a request to the agent, not a guarantee about what any agent will do: a person relying on an agent's summary of a public repository should keep that in mind.
- **A review shown in full is not vouched for.** `factory_review` gives what an account posted as a review record. It does not check that the account may review, or that the gate counted the record; on a public repository anyone can post one. What the gate decided is in the change's story. Only a comment that is a review record can be read this way, not any comment.
- **It reads as you.** The connector uses your GitHub access, so it can read whatever you can in that one repository, and nothing leaves your machine except its requests to GitHub. It makes only read requests.
- **Each question reads GitHub afresh.** A busy repository takes a few seconds, and many questions in a row count against your hourly limit with GitHub.

## What is not built

- **A hosted connector**, for agents that cannot start a program on your machine (a phone, a browser).
- **More questions**: trends over time, which parts of a repository are sent back most, how the automatic reviewer compares with people. They need more history from real use than there is yet.

## The protocol, for anyone connecting something else

One JSON-RPC message to a line on standard input, one reply to a line on standard output, UTF-8, and nothing else on standard output. It answers `initialize` (protocol revisions 2025-11-25, 2025-06-18, 2025-03-26 and 2024-11-05; asked for another, it offers its newest), `ping`, `tools/list` and `tools/call`. It does not speak the newer revision that has no `initialize`; a client that probes for it gets "method not found", with the revisions it does speak, and falls back. A question that cannot be answered (a number that is not a change, no access) comes back as a tool result marked as an error, in one sentence.
